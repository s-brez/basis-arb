from ftx_rest import FtxRestClient
from ftx_ws import FtxWebsocketClient

from collections import defaultdict
from datetime import datetime
from time import sleep
import os
import json
import sys


MARKET = ("BTC/USD", "BTC-PERP", 0.0001)    # (spot, perp, min order)
BASIS_THRESHOLD = 1                         # %
ACCOUNT_VALUE_USD = 200


def run():

    api_key = os.environ['BASIS_API_KEY_FTX']
    api_secret = os.environ['BASIS_API_SECRET_FTX']
    if api_key is None or api_secret is None:
        raise ValueError('API keys not found.')

    ws = FtxWebsocketClient(api_key, api_secret, "SpotPerpAlgo")
    rest = FtxRestClient(api_key, api_secret, "SpotPerpAlgo")
    if not ws or not rest:
        raise ModuleNotFoundError('Websocket and/or REST client failed to init.')

    valid_symbols = [m['name'] for m in rest.get_markets()]
    if MARKET[0] not in valid_symbols or MARKET[1] not in valid_symbols:
        raise ValueError('Target market symbol(s) invalid. Check ticker codes and restart program.')

    if rest.get_positions() or rest.get_open_orders():
        error_message = "Existing positions and/or orders detected. Close all positions, orders and margin borrows then restart program. \
                         Ensure collateral is denominated in an asset you will not be trading e.g hold Tether if trading BTC spot and BTC perpetual, dont hold BTC or USD."
        raise Exception(error_message)

    # Update funding initially then every hour thereafter
    borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
    funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)

    positions, orders = {}, {}
    should_run, should_update = True, False
    last_update_time = defaultdict(int)
    ob_spot = ws.get_orderbook(MARKET[0])
    ob_perp = ws.get_orderbook(MARKET[1])

    while(should_run):
        if ws and rest:

            # -----------------------------------------------------------------
            # 1. Update orders and position state with ws.get_orders() messages

            # Dont action updates older than last_actioned timestamp for a given order id
            order_updates = ws.get_orders()
            if len(order_updates) > 0:
                for oId in list(order_updates.keys()):

                    try:
                        # print(order_updates[oId]['msg_time'])
                        # print(last_update_time[oId])
                        should_update = True if order_updates[oId]['msg_time'] > last_update_time[oId] else False
                        # print("should update:", should_update)
                    except KeyError:
                        orders[oId] = order_updates[oId]
                        last_update_time[oId] = order_updates[oId]['msg_time']
                        should_update = True

                    if should_update:
                        print('update message to be actioned: ', order_updates[oId])

                        # Placement
                        if order_updates[oId]['status'] == 'new' and order_updates[oId]['filledSize'] == 0.0:
                            print("Created a new order")
                            orders[oId] = order_updates[oId]
                            last_update_time[oId] = order_updates[oId]['msg_time']

                        # Cancellation
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['filledSize'] == 0.0:
                            try:
                                print("Cancelled an existing order.")
                                del orders[oId]
                                del last_update_time[oId]
                            except KeyError:
                                print("Warning: Cancellation of unknown order detected. Manually verify positions, orders and exposure are safe. Close all positions and orders and restart program.")

                        # Complete fill
                        elif order_updates[oId]['status'] == 'closed' and order_updates[oId]['size'] == order_updates[oId]['filledSize']:

                            # Balance against existing position
                            market = order_updates[oId]['market']
                            if market in positions.keys():
                                if order_updates[oId]['side'] == positions[market]['side']:
                                    print("Increasing existing position size")
                                    positions[market]['size'] += order_updates[oId]['size']
                                else:
                                    print("Removing existing postion")
                                    positions[market]['size'] -= order_updates[oId]['size']
                                    if positions[market]['size'] == 0.0:
                                        del positions[market]
                                last_update_time[oId] = order_updates[oId]['msg_time']

                            # Create new position record if none exists
                            else:
                                print("creating new position")
                                positions[market] = {'future': market, 'size': order_updates[oId]['filledSize'], 'side': order_updates[oId]['side'], 'entryPrice': order_updates[oId]['avgFillPrice']}
                                last_update_time[oId] = order_updates[oId]['msg_time']

                            try:
                                del orders[oId]
                                del last_update_time[oId]
                            except KeyError:
                                pass

                    should_update = False

            # -----------------------------------------------------------------
            # 2. Monitor price and funding changes for entry & exit conditions

            print(int(datetime.now().timestamp()))
            if int(datetime.now().timestamp()) % 3600 == 0:
                borrow = round(float([b['estimate'] for b in rest.get_borrow_rates() if b['coin'] == MARKET[0].split('/')[0]][0] * 100), 4)
                funding = round(float(rest.get_funding_rates(MARKET[1])[0]['rate']) * 100, 4)

            ob_spot = ws.get_orderbook(MARKET[0])
            ob_perp = ws.get_orderbook(MARKET[1])
            spot_ask, spot_bid = ob_spot['asks'][0], ob_spot['bids'][0]
            perp_ask, perp_bid = ob_perp['asks'][0], ob_perp['bids'][0]
            t_price_spot = (spot_ask[0] + spot_bid[0]) / 2
            t_price_perp = (perp_ask[0] + perp_bid[0]) / 2
            if t_price_perp > t_price_spot:                 # Short perp if funding positive and perp above spot.
                basis = round(((perp_ask[0] - spot_bid[0]) / ((perp_ask[0] + spot_bid[0]) / 2)) * 100, 5)
            else:                                           # Long perp if funding negative and perp below spot.
                basis = round(((spot_ask[0] - perp_bid[0]) / ((spot_ask[0] + perp_bid[0]) / 2)) * 100, 5)

            print("----------------- " + MARKET[0] + ":" + MARKET[1] + " -----------------")
            print("Margin borrow rate (%):                    ", borrow)
            print("Perpetual funding rate (%):                ", funding)
            print("Basis (%):                                 ", basis)

            print("\nActive positions:", str(len(positions)))
            print("Ticker ---- Direction ---- Entry ---- Size ----")
            for p in positions.values():
                # print(f"{p['future']}     {p['side']}      {p['entryPrice']}    {p['size']}")
                # print(json.dumps(p, indent=2))
                print(p)

            # print("\n\nPOSITION BASIS")
            # print("MARKET 1 ---- MARKET 2 ---- BASIS (%) ---- DELTA ----")

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
