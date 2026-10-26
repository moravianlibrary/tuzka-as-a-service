"""``python -m app.workers.poller``"""

import asyncio

from app.workers.poller.worker import main

asyncio.run(main())
