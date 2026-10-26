import os

import uvicorn


def main() -> None:
    uvicorn.run("flowpilot.api.main:app", host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
