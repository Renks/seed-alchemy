import os

os.environ["DISABLE_TELEMETRY"] = "1"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import time
import uvicorn
import nest_asyncio
from pyngrok import ngrok

ngrok_tunnel = ngrok.connect(8000)
print('Public URL:', ngrok_tunnel.public_url)
nest_asyncio.apply()

start_time = time.perf_counter()
from .main import app

end_time = time.perf_counter()
elapsed_time = end_time - start_time

print(f"Startup in {elapsed_time:.6f} seconds")

ngrok_tunnel = ngrok.connect(8000)
print('Public URL:', ngrok_tunnel.public_url)
nest_asyncio.apply()
uvicorn.run(app, host="0.0.0.0", port=8000)
