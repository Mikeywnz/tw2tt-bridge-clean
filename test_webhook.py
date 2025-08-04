from fastapi import FastAPI, Request
import time
import uvicorn

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    print(f"[{time.time()}] Webhook called")
    data = await request.json()
    print(f"Payload: {data}")
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
    