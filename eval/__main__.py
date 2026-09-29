"""Run the eval: python -m eval"""

import asyncio

from eval.run import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
