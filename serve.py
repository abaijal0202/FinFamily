"""Local server entry point (used by start_finfamily.bat).

Boots the app with waitress (works on Windows, unlike gunicorn), then kicks
off a background valuation refresh so NAVs are current by the time you look
at the dashboard — per the weekly-use model: refresh on start, not on a
daily schedule.
"""
import os
import webbrowser

from dotenv import load_dotenv

load_dotenv()

from app import create_app          # noqa: E402 (needs env loaded first)
import valuation                    # noqa: E402


def main():
    app = create_app()
    port = int(os.environ.get("PORT", 8000))

    # NAV refresh + snapshot in the background; UI is usable immediately.
    valuation.start_background_refresh(app)

    url = f"http://127.0.0.1:{port}/"
    print(f"FinFamily running at {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    from waitress import serve
    serve(app, host="127.0.0.1", port=port, threads=6)


if __name__ == "__main__":
    main()
