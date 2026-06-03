import sys

if __name__ == "__main__":
    print(
        "Usage: python -m app.workers.submit | python -m app.workers.poller | python -m app.workers.cleanup"
    )
    sys.exit(1)
