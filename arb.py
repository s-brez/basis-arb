from ftx_rest import FtxRestClient
from ftx_ws import FtxWebsocketClient

from collections import defaultdict
from datetime import datetime
from time import sleep
import os
import json
import sys


MARKET = ("BTC/USD", "BTC-PERP", 0.0001)    # (spot, perp, min order)
SUBACCOUNT = "SpotPerpAlgo"                 # FTX subaccount name
BASIS_THRESHOLD = 0                         # Lowest % basis that qualifies for an entry
ORDERS_PER_SIDE = 5                         # Number of staggered orders used to reach max size when opening a position
MAX_OPEN_SIZE = 200                         # Maximum aggregate position size (all positions)
APR_EXIT_THRESHOLD = 50                     # Exit a position if funding exceeds this value

# Stop adding to positions once total open size reaches this value
SIZE_LIMIT = (MAX_OPEN_SIZE / ORDERS_PER_SIDE) * (ORDERS_PER_SIDE - 1)


def get_total_open_size(positions: dict, last_price_spot) -> float:
    size = 0
    for p in positions.values():
        size += p['size'] * last_price_spot if p['type'] == 'spot' else p['size']
    return size


def run():

    # -----------------------------------------------------------------
    # 1. Validate inputs and verify connection

    # Load keys
    api_key = os.environ['BASIS_API_KEY_FTX']
    api_secret = os.environ['BASIS_API_SECRET_FTX']
    if api_key is None or api_secret is None:
        raise ValueError('API keys not found.')

    # Init connection
    ws = FtxWebsocketClient(api_key, api_secret, SUBACCOUNT)
    rest = FtxRestClient(api_key, api_secret, SUBACCOUNT)
    if not ws or not rest:
        raise ModuleNotFoundError('Websocket and/or REST client failed to init.')

    # Validate instrument symbols
    valid_tickers = [m['name'] for m in rest.get_markets()]
    if MARKET[0] not in valid_tickers or MARKET[1] not in valid_tickers:
        raise ValueError('Target market symbol(s) invalid. Check ticker codes and restart program.')

    # Validate account starting state
    if rest.get_positions() or rest.get_open_orders():
        error_message = "Existing positions and/or orders detected. Close all positions, orders and margin borrows then restart program." \
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

    # Pre-fetch funding rates
    borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
    funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)

    positions, orders = {}, {}
    last_update_time = defaultdict(int)
    should_run, should_update = True, False
    should_add_to_position = False
    waiting_for_fill = False

    while(should_run):
        if ws and rest:

            # -----------------------------------------------------------------
            # 2. Update order and position state with ws.get_orders() messages

            order_updates = ws.get_orders()
            if len(order_updates) > 0:
                for oId in list(order_updates.keys()):

                    # Dont action updates older than last_actioned timestamp for a given order id
                    try:
                        # print(order_updates[oId]['msg_time'])
                        # print(last_update_time[oId])
                        should_update = True if order_updates[oId]['msg_time'] > last_update_time[oId] else False
                        # print("should update:", should_update)
                    except KeyError:
                        orders[oId] = order_updates[oId]
                        last_update_time[oId] = order_updates[oId]['msg_time']
                        should_update = True

                    # Match order updates to action case
                    if should_update:
                        print('update message to be actioned: ', order_updates[oId])

                        # Placement
                        if order_updates[oId]['status'] == 'new' and order_updates[oId]['filledSize'] == 0.0:
                            print("Created a new order")
                            orders[oId] = order_updates[oId]
                            last_update_time[oId] = order_updates[oId]['msg_time']
                            waiting_for_fill = True

                        # Cancellation
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['filledSize'] == 0.0:
                            try:
                                print("Cancelled an existing order.")
                                last_update_time[oId] = order_updates[oId]['msg_time']
                                del orders[oId]
                            except KeyError:
                                print("Warning: Cancellation of unknown order detected. Manually verify positions, orders and exposure are safe. Close all positions and orders and restart program.")
                                last_update_time[oId] = order_updates[oId]['msg_time']
                            waiting_for_fill = False

                        # Complete fill
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['size'] == order_updates[oId]['filledSize']:

                            # Balance against existing position
                            should_hedge = False
                            ticker = order_updates[oId]['market']
                            if ticker in positions.keys():
                                if order_updates[oId]['side'] == positions[ticker]['side']:
                                    print("Increasing existing position size")
                                    positions[ticker]['size'] += order_updates[oId]['size']
                                    should_hedge = True

                                else:
                                    print("Removing existing postion")
                                    positions[ticker]['size'] -= order_updates[oId]['size']
                                    if positions[ticker]['size'] == 0.0:
                                        del positions[ticker]
                                last_update_time[oId] = order_updates[oId]['msg_time']

                            # Create new position record if none exists
                            else:
                                print("creating new position")
                                instrument_type = 'spot' if ticker == MARKET[0] else "perp"
                                positions[ticker] = {'future': ticker, 'type': instrument_type, 'size': order_updates[oId]['filledSize'], 'side': order_updates[oId]['side'], 'entryPrice': order_updates[oId]['avgFillPrice']}
                                last_update_time[oId] = order_updates[oId]['msg_time']
                                should_hedge = True

                            try:
                                del orders[oId]
                                del last_update_time[oId]
                            except KeyError:
                                pass

                            # If this was a spot fill send a perp market order and vice versa
                            if should_hedge:
                                pass

                            waiting_for_fill = False

                    should_update = False

            # -----------------------------------------------------------------
            # 3. Action price and funding changes

            # Refresh funding every 5 min
            # ts = int(datetime.now().timestamp())
            # print(ts, ts % 300)
            if int(datetime.now().timestamp()) % 300 == 0:
                borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
                funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)
                print("UPDATING FUNDING")
                # print(sys.exit(0))

            # Refresh prices and calculate basis
            ob_spot, ob_perp = ws.get_orderbook(MARKET[0]), ws.get_orderbook(MARKET[1])
            spot_ask, spot_bid = ob_spot['asks'][0], ob_spot['bids'][0]
            perp_ask, perp_bid = ob_perp['asks'][0], ob_perp['bids'][0]
            last_price_spot, last_price_perp = ws.get_ticker(MARKET[0])['last'], ws.get_ticker(MARKET[1])['last']
            if last_price_perp > last_price_spot:  # Short perp if funding positive and perp above spot.
                basis = round(((perp_ask[0] - spot_bid[0]) / ((perp_ask[0] + spot_bid[0]) / 2)) * 100, 5)
                print("perp is above spot")
            else:                                  # Long perp if funding negative and perp below spot.
                basis = round(((spot_ask[0] - perp_bid[0]) / ((spot_ask[0] + perp_bid[0]) / 2)) * 100, 5)
                print("spot is above perp")

            # Take action according to basis and position/order state
            total_open_size = get_total_open_size(positions, last_price_spot)
            position_count = len(positions)
            order_count = len(orders)

            print("waiting_for_fill:", waiting_for_fill)
            print("should_add_to_position:", should_add_to_position)
            print("total open size:", total_open_size)
            print("size limit:", SIZE_LIMIT)

            if not waiting_for_fill:

                if basis >= BASIS_THRESHOLD:

                    # No existing positions or open orders
                    if position_count == 0 and order_count == 0:
                        should_add_to_position = True

                    # Existing positions, no open orders and under max size limit
                    elif position_count > 0 and order_count == 0 and total_open_size <= SIZE_LIMIT:
                        should_add_to_position = True

                    # No existing positions, open orders and under max size limit
                    elif position_count == 0 and order_count > 0:
                        should_add_to_position = False

                else:
                    should_add_to_position = False

                if should_add_to_position:
                    if total_open_size <= MAX_OPEN_SIZE - (MAX_OPEN_SIZE / 5):
                        print()

                    # Partial position and open order: wait for the open order to fill.
                    if position_count > 0 and order_count > 0:
                        pass

            # Actions to take when waiting for fill
            else:
                pass

            # Exit an open position if perp funding unfavourable and APR threshold reached
            if True:
                pass

            print("----------------- " + MARKET[0] + ":" + MARKET[1] + " -----------------")
            print("Spot margin borrow APR:                   ", borrow * 8760)
            print("Perpetual funding APR:                    ", funding * 8760)
            print("Spot/perp basis %:                        ", basis)

            print("\nActive positions:", str(len(positions)))
            print("Ticker ---- Direction ---- Entry ---- Size ----")
            for p in positions.values():
                # print(f"{p['future']}     {p['side']}      {p['entryPrice']}    {p['size']}")
                # print(json.dumps(p, indent=2))
                print(p)

            print("\nOpen orders:" + str(len(orders)))
            print("Ticker ---- Direction ---- Entry ---- Size ----")
            for o in orders.values():
                print(o)
                    # print(f"{o['market']}     {o['side']}       {o['size']}    {o['price']}")

            print("\n\n")
            # SPOT SIZE IS DENOTED IN BTC NOT USD! IMPORTANT
            # market = "BTC/USD"
            # side = "sell"
            # price = spot_ask[0] * 1.0005
            # size = 0.0001
            # type = 'limit'
            # reduce_only = False
            # ioc = False
            # post_only = False
            # client_id = None
            # reject_after_ts = None
            # print(json.dumps(
            #     rest.place_order(market, side, price, size, type, reduce_only, ioc, post_only, client_id, reject_after_ts), indent=2))

            # market = "BTC-PERP"
            # side = "sell"
            # price = None
            # size = 0.0025
            # type = 'market'
            # reduce_only = False
            # ioc = False
            # post_only = False
            # client_id = None
            # reject_after_ts = None
            # print(json.dumps(
            #     rest.place_order(market, side, price, size, type, reduce_only, ioc, post_only,
            #                      client_id, reject_after_ts), indent=2))

            # market = "BTC-PERP"
            # side = "buy"
            # price = None
            # size = 0.0025
            # type = "market"
            # reduce_only = False
            # ioc = False
            # post_only = False
            # client_id = None
            # reject_after_ts = None
            # print(json.dumps(
            #     rest.place_order(market, side, price, size, type, reduce_only, ioc, post_only, client_id, reject_after_ts), indent=2))

            # print(json.dumps(rest.get_order_history(), indent=2))
            # print(json.dumps(rest.get_open_orders(), indent=2))
            # print("\norder updates")
            # print(json.dumps(ws.get_orders(), indent=2))
            # print(rest.get_positions())

            sleep(5)

        else:
            if not ws:
                ws = FtxWebsocketClient(api_key, api_secret)
            if not rest:
                rest = FtxRestClient(api_key, api_secret, None)

run()
