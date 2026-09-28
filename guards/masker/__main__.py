"""Run the Masker on 127.0.0.1:10003."""

import uvicorn

from guards.masker.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=10003)
