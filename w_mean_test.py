positions = {
    'BTC-PERP': {'ticker': 'BTC-PERP', 'type': 'perp', 'size': 0.0018, 'side': 'buy', 'avgEntryPrice': 13288.833333333334, 'fillCount': 2},
    'BTC/USD': {'ticker': 'BTC/USD', 'type': 'spot', 'size': 0.0012, 'side': 'sell', 'avgEntryPrice': 19939.5, 'fillCount': 3}
}

p_count = len(positions)
p_list = list(positions.values())
if p_count % 2 == 0:
    if p_list[0]['fillCount'] != p_list[1]['fillCount']:
        result = True
    else:
        result = False
elif p_count == 0:
    result = False
else:
    result = True

print(result)
