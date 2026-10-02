import sys


class Progress:
    "progress reporting shared by the command line and the GUI"

    def status(self, message: str) -> None:
        print(message)

    def warning(self, message: str) -> None:
        print(f"WARNING: {message}", file=sys.stderr)

    def update(self, done: int, total: int) -> None:
        pass
