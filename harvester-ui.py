#!/usr/bin/env python3
"""Launch the optional Harvester Severin desktop frontend."""

from harvester_ui import PACKAGE_ID, PROJECT_DIR, make_bridge_callback


def main():
    # Severin is an optional headed capability, so importing this launcher is
    # harmless on CLI-only installations and the dependency is loaded only here.
    import severin

    app_box = {}
    bridge = make_bridge_callback(app_box)
    app = severin.App(width=800, height=400, bridge=bridge, package_id=PACKAGE_ID)
    app_box["app"] = app
    try:
        app.load_path(str(PROJECT_DIR / "index.html"))
        app.run()
    finally:
        app_box["app"] = None
        app.close()


if __name__ == "__main__":
    main()
