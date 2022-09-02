from ftx_rest import FtxRestClient
from ftx_ws import FtxWebsocketClient

from collections import defaultdict
from time import sleep
import os
import json
import sys


MARKET = ("BTC/USD", "BTC-PERP")
ACCOUNT_VALUE_USD = 100


def run():

    api_key = os.environ['BASIS_API_KEY_FTX']
    api_secret = os.environ['BASIS_API_SECRET_FTX']
    if api_key is None or api_secret is None:
        raise ValueError('API keys not found.')

    ws = FtxWebsocketClient(api_key, api_secret, "SpotPerpAlgo")
    rest = FtxRestClient(api_key, api_secret, "SpotPerpAlgo")

    if not ws or not rest:
        raise ModuleNotFoundError('Websocket and/or REST client failed to init.')

    if rest.get_positions() or rest.get_open_orders():
        print("Existing positions and/or orders detected. Close all positions and orders then restart program.")
        sys.exit(0)

    positions, orders = {}, {}
    should_run, should_update = True, False
    last_update_time = defaultdict(int)

    while(should_run):
        if ws and rest:

            # Refresh order state
            order_updates = ws.get_orders()
            if len(order_updates) > 0:
                for oId in list(order_updates.keys()):

                    # Dont action updates older than last_actioned timestamp
                    try:
                        print(order_updates[oId]['msg_time'])
                        print(last_update_time[oId])
                        should_update = True if order_updates[oId]['msg_time'] > last_update_time[oId] else False
                        print("should update:", should_update)
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


            print("\n\nACTIVE POSITIONS (" + str(len(positions)) + ")")
            print("MARKET ---- SIDE ---- ENTRY ---- SIZE ----")
            for p in positions.values():
                # if p['size'] > 0 or p['size'] < 0:
                    # print(f"{p['future']}     {p['side']}      {p['entryPrice']}    {p['size']}")
                    # print(json.dumps(p, indent=2))
                    print(positions)

            # print("\n\nPOSITION BASIS")
            # print("MARKET 1 ---- MARKET 2 ---- BASIS (%) ---- DELTA ----")

            print("\nOPEN ORDERS (" + str(len(orders)) + ")")
            print("MARKET ---- SIDE ---- SIZE ---- PRICE ----")
            # if len(orders) > 0:
            #     print(orders)
                # for o in orders.values():
                #     print(o)
                    # print(f"{o['market']}     {o['side']}       {o['size']}    {o['price']}")

            sleep(5)

        else:
            if not ws:
                ws = FtxWebsocketClient(api_key, api_secret)
            if not rest:
                rest = FtxRestClient(api_key, api_secret, None)


run()
