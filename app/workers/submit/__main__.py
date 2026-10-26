"""``python -m app.workers.submit``"""

import asyncio

from app.workers.submit.worker import main

asyncio.run(main())
