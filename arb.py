from ftx_rest import FtxRestClient
from ftx_ws import FtxWebsocketClient

from collections import defaultdict
from statistics import fmean
from datetime import datetime
from time import sleep
import numpy as np
import keyboard
import sys
import os


SUBACCOUNT = "SpotPerpAlgo"                 # Subaccount name
MARKET = ("BTC/USD", "BTC-PERP", 0.0001)    # (spot ticker, perp ticker, min size increment) Spot must be first and perp second
ACCOUNT_SIZE = 130                          # Maximum combined size for both positions
ORDERS_PER_SIDE = 3                         # Number of staggered orders used to reach max size when opening a position
DEFAULT_BASIS_THRESHOLD = 0.0005            # Smallest percentage basis that qualifies for an entry
MARGIN_FOR_ENTRY = 0.5                      # Allowable percentage reduction from initial basis for subsequent entries

BASIS_FLOOR = 0.005                         # If basis reaches or goes lower than this, convergence of spot and future price is considered to have ocurred.
BAD_ENTRY_CUTOFF = 2                        # % distance past profitable at which an attempted entry is considered failed.
APR_EXIT_THRESHOLD = 50                     # Exit a position if funding exceeds this value in the wrong direction

QUOTE_INDEX = 1                             # Bid/ask index used for limit order pricing. 0 means 1st level, 1 means 2nd level and so on.
MOVE_ORDER_THRESHOLD = 2                    # Move a limit order to follow price if it moves this many OB levels away from last price

DEBUG_OUTPUT = True                         # If True program actions print to console


# Return total size of all positions
def get_total_open_size(positions: dict) -> float:
    size = 0
    if positions:
        for p in positions.values():
            size += p['size'] * p['avgEntryPrice']
    return size


# Return ticker and direction needing a size increase to zero out net exposure
def has_exposure(positions: dict) -> [str, str, str]:
    p_list = list(positions.values())
    p_count = len(p_list)
    if p_list:
        if p_count % 2 == 0:
            must_hedge = [None, None, None]
            if p_list[0]['fillCount'] > p_list[1]['fillCount'] or p_list[0]['fillCount'] < p_list[1]['fillCount']:
                must_hedge[0] = p_list[1]['ticker'] if p_list[0]['fillCount'] > p_list[1]['fillCount'] else p_list[0]['ticker']
                must_hedge[1] = p_list[1]['side'] if p_list[0]['fillCount'] > p_list[1]['fillCount'] else p_list[0]['side']
                must_hedge[2] = "perp" if p_list[0]['fillCount'] > p_list[1]['fillCount'] else "spot"
            else:
                must_hedge = None
        elif p_count == 1:
            must_hedge = [None, None, None]
            must_hedge[0] = MARKET[1] if p_list[0]['ticker'] == MARKET[0] else MARKET[0]
            must_hedge[1] = 'buy' if p_list[0]['side'] == 'sell' else 'sell'
            must_hedge[2] = 'spot' if p_list[0]['type'] == 'perp' else 'perp'
        elif p_count == 0:
            must_hedge = None
    else:
        must_hedge = None
    return must_hedge


# Return total fills of all positions
def get_total_fills(positions: dict) -> float:
    fills = 0
    if positions:
        for p in positions.values():
            fills += p['fillCount']
    return fills


def run():

    # -----------------------------------------------------------------
    # 1. Validate inputs and verify connection
    # -----------------------------------------------------------------

    # Load keys
    api_key = os.environ['BASIS_API_KEY_FTX']
    api_secret = os.environ['BASIS_API_SECRET_FTX']
    if api_key is None or api_secret is None:
        raise ValueError('API keys not found.')

    # Init connection clients
    ws = FtxWebsocketClient(api_key, api_secret, SUBACCOUNT)
    rest = FtxRestClient(api_key, api_secret, SUBACCOUNT)
    if not ws or not rest:
        raise ModuleNotFoundError('Websocket or REST client failed to init.')

    # Validate instrument symbols
    valid_tickers = [m['name'] for m in rest.get_markets()]
    if MARKET[0] not in valid_tickers or MARKET[1] not in valid_tickers:
        raise ValueError('Target market ticker invalid. Check ticker codes and restart program.')

    # Validate account starting state
    if rest.get_positions() or rest.get_open_orders():
        error_message = "Existing positions or orders detected. Close all positions, orders and margin borrows, then restart program." \
                        " Ensure margin collateral is denominated in an asset you will not be trading e.g hold Tether if trading BTC spot and BTC perpetual, dont hold BTC or USD."
        raise Exception(error_message)

    # Verify websocket is subscribed and receiving data
    ws_data_ready, wait_time = False, 0
    while(not ws_data_ready):
        order_updates = ws.get_orders()
        ob_spot, ob_perp = ws.get_orderbook(MARKET[0]), ws.get_orderbook(MARKET[1])
        last_price_spot, last_price_perp = ws.get_ticker(MARKET[0]), ws.get_ticker(MARKET[1])
        if ob_spot and ob_perp and last_price_spot and last_price_perp:
            ws_data_ready = True
        else:
            sleep(1)
            wait_time += 1
        if wait_time > 10:
            raise Exception("Unable to subscribe to exchange websocket channels.")

    borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
    funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)

    positions, orders = {}, {}
    last_update_time = defaultdict(int)
    should_run, should_update = True, False
    basis, start_basis = None, None
    fill_count, waiting_for_fill = 0, False
    at_max_size = False
    exposure = False
    should_add_to_positions, should_unwind_positions = False, False
    should_increase_spot, should_increase_perp = False, False
    should_reduce_spot, should_reduce_perp = False, False

    while(should_run):
        if ws and rest:

            # -----------------------------------------------------------------
            # 2. Update order and position state with ws.get_orders() messages
            # -----------------------------------------------------------------

            order_updates = ws.get_orders()
            if len(order_updates) > 0:
                for oId in list(order_updates.keys()):

                    # Dont action updates older than last_actioned timestamp for a given order id
                    try:
                        should_update = True if order_updates[oId]['msg_time'] > last_update_time[oId] else False
                    except KeyError:
                        orders[oId] = order_updates[oId]
                        last_update_time[oId] = order_updates[oId]['msg_time']
                        should_update = True

                    # Match order updates to action case
                    if should_update:

                        # Placement
                        if order_updates[oId]['status'] == 'new' and order_updates[oId]['filledSize'] == 0.0:
                            if DEBUG_OUTPUT:
                                print("Created a new order")
                            orders[oId] = order_updates[oId]
                            last_update_time[oId] = order_updates[oId]['msg_time']
                            waiting_for_fill = True

                        # Cancellation
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['filledSize'] == 0.0:
                            try:
                                del orders[oId]
                                if DEBUG_OUTPUT:
                                    print("Cancelled an existing order.")
                            except KeyError:
                                last_update_time[oId] = order_updates[oId]['msg_time']
                                raise Exception("\n\nWarning: Unexpected cancellation detected. Small order rate limits may have been exceeded. Manually verify positions, orders and exposure are safe. Close all positions and orders and restart program. If unexpected limit order cancellation persists wait 1 hour before retrying.")
                            last_update_time[oId] = order_updates[oId]['msg_time']
                            waiting_for_fill = False

                        # Complete fill
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['size'] == order_updates[oId]['filledSize']:

                            # Save initial basis, this will be referenced when determine exit conditions.
                            if not start_basis:
                                start_basis = basis

                            # Balance against existing position
                            ticker = order_updates[oId]['market']
                            fill_count += 1
                            instrument_type = 'spot' if ticker == MARKET[0] else "perp"
                            if ticker in positions.keys():

                                if order_updates[oId]['side'] == positions[ticker]['side']:
                                    if DEBUG_OUTPUT:
                                        print("Increasing existing position")
                                    positions[ticker]['size'] = round(positions[ticker]['size'] + order_updates[oId]['size'], 4)
                                    positions[ticker]['fillCount'] += 1
                                    w_pos = (positions[ticker]['fillCount'] - 1) / positions[ticker]['fillCount']
                                    w_new = 1 / positions[ticker]['fillCount']
                                    avg_entry = positions[ticker]['avgEntryPrice'] * w_pos + order_updates[oId]['avgFillPrice'] * w_new
                                    positions[ticker]['avgEntryPrice'] = round(avg_entry, 2)

                                else:
                                    if DEBUG_OUTPUT:
                                        print("Decreasing existing postion")
                                    positions[ticker]['size'] = round(positions[ticker]['size'] - order_updates[oId]['size'], 4)
                                    positions[ticker]['fillCount'] -= 1
                                    if positions[ticker]['size'] == 0.0:
                                        del positions[ticker]

                            # Create new position record if none exists
                            else:
                                if DEBUG_OUTPUT:
                                    print("creating new", instrument_type, "position")
                                positions[ticker] = {'ticker': ticker, 'type': instrument_type, 'size': order_updates[oId]['filledSize'], 'side': order_updates[oId]['side'], 'avgEntryPrice': order_updates[oId]['avgFillPrice'], 'fillCount': 1}
                            try:
                                del orders[oId]
                                del last_update_time[oId]
                            except KeyError:
                                pass

                            last_update_time[oId] = order_updates[oId]['msg_time']
                            waiting_for_fill = False
                            exposure = has_exposure(positions)

                    should_update = False

            # -----------------------------------------------------------------
            # 3. Monitor price and funding changes for entry and exit conditions
            # -----------------------------------------------------------------

            # Refresh funding rates every 5 min
            if int(datetime.now().timestamp()) % 300 == 0:
                borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
                funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)

            # Update prices and calculate basis
            ob_spot, ob_perp = ws.get_orderbook(MARKET[0]), ws.get_orderbook(MARKET[1])
            spot_ask, spot_bid = ob_spot['asks'][0], ob_spot['bids'][0]
            perp_ask, perp_bid = ob_perp['asks'][0], ob_perp['bids'][0]
            last_price_spot, last_price_perp = ws.get_ticker(MARKET[0])['last'], ws.get_ticker(MARKET[1])['last']
            if last_price_perp > last_price_spot:
                basis = round(((perp_ask[0] - spot_bid[0]) / ((perp_ask[0] + spot_bid[0]) / 2)) * 100, 5)
                perp_above_spot = True
                above_below_message = "Perpetual is above Spot"
            else:
                basis = round(((spot_ask[0] - perp_bid[0]) / ((spot_ask[0] + perp_bid[0]) / 2)) * 100, 5)
                perp_above_spot = False
                above_below_message = "Spot is above Perpetual"

            total_open_size = get_total_open_size(positions)
            position_count, order_count = len(positions), len(orders)
            total_fills = get_total_fills(positions)

            basis_threshold = DEFAULT_BASIS_THRESHOLD if position_count == 0 else DEFAULT_BASIS_THRESHOLD * MARGIN_FOR_ENTRY

            if total_open_size >= ACCOUNT_SIZE or total_fills == ORDERS_PER_SIDE * 2:
                at_max_size = True
            else:
                at_max_size = False

            if at_max_size:
                should_add_to_positions = False
                should_increase_spot = False
                should_increase_perp = False
                waiting_for_fill = False

            if position_count == 2:

                # Exit criteria 1: positioned, basis converges.
                if (positions[MARKET[1]]['side'] == 'buy' and funding > 0) or (positions[MARKET[1]]['side'] == 'sell' and funding < 0):
                    print("-------------------------------------------------------")
                    print("START EXITING POSITIONS")
                    print("Basis convergence")
                    print("-------------------------------------------------------")
                    should_unwind_positions = True
                    should_add_to_positions = False
                    waiting_for_fill = False

                # Exit criteria 2: positioned, basis valid, but funding APR worse than acceptable.
                if (positions[MARKET[1]]['side'] == 'buy' and funding > 0 and abs(funding) >= APR_EXIT_THRESHOLD) or (positions[MARKET[1]]['side'] == 'sell' and funding < 0 and abs(funding) >= APR_EXIT_THRESHOLD):
                    print("-------------------------------------------------------")
                    print("START EXITING POSITIONS")
                    print("Unfavourable funding APR")
                    print("-------------------------------------------------------")
                    should_unwind_positions = True
                    should_add_to_positions = False
                    waiting_for_fill = False

                # Exit criteria 3: arbitrary manual exit
                if keyboard.is_pressed('ctrl+enter'):
                    print("-------------------------------------------------------")
                    print("START EXITING POSITIONS")
                    print("Manual exit signal")
                    print("-------------------------------------------------------")
                    should_unwind_positions = True
                    should_add_to_positions = False
                    waiting_for_fill = False


                # For debug only - triggers position unwind as soon as max size is reached.
                # if fill_count == ORDERS_PER_SIDE * 2 and not waiting_for_fill and order_count == 0 and not should_unwind_positions:
                #     print("-------------------------------------------------------")
                #     print("START EXITING POSITIONS")
                #     print("-------------------------------------------------------")
                #     should_unwind_positions = True
                #     should_add_to_positions = False
                #     waiting_for_fill = False

            if DEBUG_OUTPUT:
                print("waiting_for_fill:", waiting_for_fill)
                print("should_add_to_positions:", should_add_to_positions)
                print("should_unwind_positions:", should_unwind_positions)
                print("total open size:", total_open_size)
                print("account size:", ACCOUNT_SIZE)
                print("at_max_size:", at_max_size)
                print("exposure:", exposure)

            # Add to positions
            if not should_unwind_positions and not at_max_size:
                if not waiting_for_fill:
                    if abs(basis) >= basis_threshold:
                        if (perp_above_spot and funding > 0) or (not perp_above_spot and funding < 0):
                            should_add_to_positions = True
                        else:
                            should_add_to_positions = False
                            print("No entry conditions detected.")
                    else:
                        should_add_to_positions = False
                        print("Basis too small.")

                    if should_add_to_positions and not at_max_size:

                        # Place limit orders on both sides such that exposure is balanced by the next fill
                        try:
                            if order_count == 0:
                                if position_count == 0 or positions[MARKET[0]]["fillCount"] == positions[MARKET[1]]["fillCount"]:
                                    should_increase_spot = True
                                    should_increase_perp = True
                                elif positions[MARKET[0]]["fillCount"] > positions[MARKET[1]]["fillCount"]:
                                    should_increase_spot = False
                                    should_increase_perp = True
                                elif positions[MARKET[0]]["fillCount"] < positions[MARKET[1]]["fillCount"]:
                                    should_increase_spot = True
                                    should_increase_perp = False

                            elif order_count == 1:
                                try:
                                    if positions[MARKET[0]]["fillCount"] > positions[MARKET[1]]["fillCount"]:
                                        should_increase_spot = False
                                        should_increase_perp = True
                                    elif positions[MARKET[0]]["fillCount"] < positions[MARKET[1]]["fillCount"]:
                                        should_increase_spot = True
                                        should_increase_perp = False
                                    elif positions[MARKET[0]]["fillCount"] == positions[MARKET[1]]["fillCount"]:
                                        if list(orders.values())[0]['market'] == MARKET[0]:
                                            should_increase_perp = True
                                        else:
                                            should_increase_spot = True

                                # Place order on side of missing position, if any.
                                except KeyError as missing_ticker:
                                    if DEBUG_OUTPUT:
                                        print("inner no position for", missing_ticker)
                                    if MARKET[0] == missing_ticker:
                                        should_increase_perp = False
                                        should_increase_spot = True
                                    else:
                                        should_increase_perp = True
                                        should_increase_spot = False

                            # Do nothing if orders open for both positions, wait for fills on either side
                            elif order_count == 2:
                                waiting_for_fill = True

                        # Place order on side of missing position, if any.
                        except KeyError as missing_ticker:
                            if DEBUG_OUTPUT:
                                print("outer no position for", missing_ticker)
                            if position_count == 0:
                                should_increase_perp = True
                                should_increase_spot = True
                            elif MARKET[0] == missing_ticker:
                                should_increase_perp = False
                                should_increase_spot = True
                            elif MARKET[1] == missing_ticker:
                                should_increase_perp = True
                                should_increase_spot = False

                        if DEBUG_OUTPUT:
                            print("should increase spot:", should_increase_spot)
                            print("should increase perp:", should_increase_perp)

                        if should_increase_spot and MARKET[0] not in [o['market'] for o in orders.values()]:
                            if DEBUG_OUTPUT:
                                print("increase spot position")
                            base_size = ACCOUNT_SIZE / ORDERS_PER_SIDE / 2 / last_price_spot
                            size = round(MARKET[2] * round(float(base_size) / MARKET[2]), 4)
                            try:
                                side = 'sell' if positions[MARKET[0]]['side'] == 'sell' else 'buy'
                            except KeyError:
                                side = 'sell' if not perp_above_spot else 'buy'
                            price = ws.get_orderbook(MARKET[0])['bids'][QUOTE_INDEX][0] if side == 'buy' else ws.get_orderbook(MARKET[0])['asks'][QUOTE_INDEX][0]
                            if DEBUG_OUTPUT:
                                print("Placing spot entry order:", size, side, price)
                                quotes = ws.get_orderbook(MARKET[0])['bids'][0:5] if side == 'buy' else ws.get_orderbook(MARKET[0])['asks'][0:5]
                                print("Spot quotes 0-5", quotes)
                            rest.place_order(MARKET[0], side, price, size, "limit", False, False, False, None, None)
                            should_increase_spot = False

                        if should_increase_perp and MARKET[1] not in [o['market'] for o in orders.values()]:
                            if DEBUG_OUTPUT:
                                print("increase perp position")
                            base_size = ACCOUNT_SIZE / ORDERS_PER_SIDE / 2 / last_price_perp
                            size = round(MARKET[2] * round(float(base_size) / MARKET[2]), 4)
                            try:
                                side = 'sell' if positions[MARKET[1]]['side'] == 'sell' else 'buy'
                            except KeyError:
                                side = 'sell' if perp_above_spot else 'buy'
                            price = ws.get_orderbook(MARKET[1])['bids'][QUOTE_INDEX][0] if side == 'buy' else ws.get_orderbook(MARKET[1])['asks'][QUOTE_INDEX][0]
                            if DEBUG_OUTPUT:
                                print("Placing perp entry order:", size, side, price)
                                quotes = ws.get_orderbook(MARKET[1])['bids'][0:5] if side == 'buy' else ws.get_orderbook(MARKET[1])['asks'][0:5]
                                print("Perp quotes 0-5", quotes)
                            rest.place_order(MARKET[1], side, price, size, "limit", False, False, False, None, None)
                            should_increase_perp = False

            # Unwind open positions
            elif should_unwind_positions:
                if not waiting_for_fill:
                    if position_count > 0:

                        # Place exit orders on both sides if exposure is balanced
                        try:
                            if order_count == 0 and positions[MARKET[0]]["fillCount"] == positions[MARKET[1]]["fillCount"]:
                                should_reduce_spot = True
                                should_reduce_perp = True

                            # Place one order on the side with greater exposure
                            elif order_count == 1:
                                try:
                                    # If spot position larger than perp
                                    if positions[MARKET[0]]["fillCount"] > positions[MARKET[1]]["fillCount"]:
                                        should_reduce_spot = True
                                        should_reduce_perp = False

                                    # If perp position larger than spot
                                    elif positions[MARKET[0]]["fillCount"] < positions[MARKET[1]]["fillCount"]:
                                        should_reduce_spot = False
                                        should_reduce_perp = True

                                except KeyError as missing_ticker:
                                    if MARKET[0] == missing_ticker:
                                        should_reduce_perp = True
                                        should_reduce_spot = False
                                    else:
                                        should_reduce_perp = False
                                        should_reduce_spot = True

                            # Do nothing if orders open for both positions, wait for fills on either side
                            elif order_count == 2:
                                waiting_for_fill = True

                            if DEBUG_OUTPUT:
                                print("should reduce spot:", should_reduce_spot)
                                print("should reduce perp:", should_reduce_perp)

                        # Place order on side of missing position, if any.
                        except KeyError as missing_ticker:
                            if MARKET[0] == missing_ticker:
                                should_reduce_perp = True
                                should_reduce_spot = False
                            else:
                                should_reduce_perp = False
                                should_reduce_spot = True

                        if should_reduce_spot and MARKET[0] not in [o['market'] for o in orders.values()]:
                            try:
                                if DEBUG_OUTPUT:
                                    print("reduce spot position")
                                size = positions[MARKET[0]]['size'] / positions[MARKET[0]]['fillCount']
                                side = 'buy' if positions[MARKET[0]]['side'] == 'sell' else 'sell'
                                price = ws.get_orderbook(MARKET[0])['bids'][QUOTE_INDEX][0] if side == 'buy' else ws.get_orderbook(MARKET[0])['asks'][QUOTE_INDEX][0]
                                if DEBUG_OUTPUT:
                                    print("Placing spot exit order:", size, side, price)
                                    print("Spot last price:", last_price_spot)
                                    quotes = ws.get_orderbook(MARKET[0])['bids'][0:5] if side == 'buy' else ws.get_orderbook(MARKET[0])['asks'][0:5]
                                    print("Spot quotes 0-5", quotes)
                                rest.place_order(MARKET[0], side, price, size, "limit", False, False, False, None, None)
                                waiting_for_fill = True
                                should_reduce_spot = False
                            except KeyError as already_closed:
                                if DEBUG_OUTPUT:
                                    print("Position for", already_closed, "already_closed")

                        if should_reduce_perp and MARKET[1] not in [o['market'] for o in orders.values()]:
                            try:
                                if DEBUG_OUTPUT:
                                    print("reduce perp position")
                                size = positions[MARKET[1]]['size'] / positions[MARKET[1]]['fillCount']
                                side = 'sell' if positions[MARKET[1]]['side'] == 'buy' else 'buy'
                                price = ws.get_orderbook(MARKET[1])['bids'][QUOTE_INDEX][0] if side == 'buy' else ws.get_orderbook(MARKET[1])['asks'][QUOTE_INDEX][0]
                                if DEBUG_OUTPUT:
                                    print("Placing perp exit order:", size, side, price)
                                    print("Perp last price:", last_price_perp)
                                    quotes = ws.get_orderbook(MARKET[1])['bids'][0:5] if side == 'buy' else ws.get_orderbook(MARKET[1])['asks'][0:5]
                                    print("Perp quotes 0-5", quotes)
                                rest.place_order(MARKET[1], side, price, size, "limit", False, False, False, None, None)
                                waiting_for_fill = True
                                should_reduce_perp = False
                            except KeyError as already_closed:
                                if DEBUG_OUTPUT:
                                    print("Position for", already_closed, "already_closed")
                    else:
                        print("\n\nTrade complete. Terminating.")
                        sys.exit(0)

            # -----------------------------------------------------------------
            # 4. Check stop-loss conditions and move open orders to follow price
            # -----------------------------------------------------------------

            exposure = has_exposure(positions)
            for o in orders.values():
                within_risk_limit = True
                last_price = last_price_perp if o['market'] == MARKET[1] else last_price_spot
                ob = ws.get_orderbook(o['market'])['asks'] if side == 'sell' else ws.get_orderbook(o['market'])['bids']
                ob_step = abs(fmean(np.diff([quote[0] for quote in ob[0:5]])))
                new_price = ob[QUOTE_INDEX][0]

                # Calculate stop distance from avg entry of opposing exposed position
                if exposure:
                    market = MARKET[0] if exposure[0] == MARKET[1] else MARKET[1]
                    side = 'sell' if exposure[1] == 'buy' else 'buy'
                    entry = positions[market]['avgEntryPrice']

                    # In future suggest using the size of the basis as the stop distance. Fixed distance is disproportionate in most cases.
                    # Using a very small basis threshold is good for testing but will mean stopping out of an entry often.
                    stop_price = entry - (entry / 100 * BAD_ENTRY_CUTOFF) if side == 'buy' else entry + (entry / 100 * BAD_ENTRY_CUTOFF)
                    if DEBUG_OUTPUT:
                        print("cutoff price for open order:", stop_price)

                    if side == 'buy' and o['price'] <= stop_price:
                        within_risk_limit = False
                    elif side == 'sell' and o['price'] >= stop_price:
                        within_risk_limit = False

                    # Close exposed portion of trade and cancel open order
                    if not within_risk_limit:
                        if DEBUG_OUTPUT:
                            print("cutoff reached. closing exposed portion of trade and cancelling open order")
                        size = positions[market]['size'] / positions[market]['fillCount']
                        rest.cancel_order(o['id'])
                        rest.place_order(market, exposure[1], None, size, "market", False, False, False, None, None)
                        should_add_to_positions = False
                        waiting_for_fill = False

                # Move open limit orders closer to price if order price is more than MOVE_ORDER_THRESHOLD levels from last price.
                if within_risk_limit and abs(o['price'] - last_price) > ob_step * MOVE_ORDER_THRESHOLD and new_price != o['price']:
                    if DEBUG_OUTPUT:
                        print("moving existing limit order")
                    rest.modify_order(o['id'], None, new_price, None, None)

            print("----------------- " + MARKET[0] + ":" + MARKET[1] + " -----------------")
            print("Spot margin borrow APR:                   ", round(borrow * 8760, 5))
            print("Perpetual funding APR:                    ", round(funding * 8760, 5))
            print("Spot/perp basis %:                        ", round(basis, 5))
            print(above_below_message)

            print("\nActive positions:", str(len(positions)))
            print("Ticker ---- Direction ---- Avg. entry ---- Size ----  Fill count ---- ")
            for p in positions.values():
                print(f"{p['ticker']}     {p['side']}             {p['avgEntryPrice']}       {p['size']}     {p['fillCount']}")
            print("\nOpen orders: " + str(len(orders)))

            print("Ticker ---- Direction ----- Price ---- Size ---- Status ----")
            for o in orders.values():
                print(f"{o['market']}     {o['side']}           {o['price']}     {o['size']}    {o['status']}")

            print("\n\n")
            sleep(4)

        else:
            if not ws:
                ws = FtxWebsocketClient(api_key, api_secret, SUBACCOUNT)
            if not rest:
                rest = FtxRestClient(api_key, api_secret, SUBACCOUNT)


run()
