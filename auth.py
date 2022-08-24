import time
import hmac
from requests import Request


ts = int(time.time() * 1000)
request = Request('GET', '<api_endpoint>')
prepared = request.prepare()
signature_payload = f'{ts}{prepared.method}{prepared.path_url}'.encode()
signature = hmac.new('YOUR_API_SECRET'.encode(), signature_payload, 'sha256').hexdigest()

prepared.headers['FTX-KEY'] = 'YOUR_API_KEY'
prepared.headers['FTX-SIGN'] = signature
prepared.headers['FTX-TS'] = str(ts)

# Only include line if you want to access a subaccount.
# Remember to URI-encode the subaccount name if it contains special characters!
# prepared.headers['FTX-SUBACCOUNT'] = 'my_subaccount_nickname'
