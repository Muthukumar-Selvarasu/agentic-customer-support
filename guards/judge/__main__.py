"""Run the Judge on 127.0.0.1:10002."""

import uvicorn

from guards.judge.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=10002)
