# Contributing to ResinPilot

Thanks for helping! Printer models, resin profiles, drivers and bug reports are all welcome.

## Development setup

```bash
git clone https://github.com/PtolemyDL/ResinPilot.git
cd ResinPilot
python -m venv .venv && .venv/Scripts/activate     # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
resinpilot --port 8790 --no-browser                 # then open http://127.0.0.1:8790
```

Set `RESINPILOT_HOME` to a scratch folder while developing so you don't touch your real print history.

Layout:

| Path                         | What                                                         |
| ---------------------------- | ------------------------------------------------------------ |
| `resinpilot/drivers/`        | Printer drivers (discovery + live status)                     |
| `resinpilot/sdcp.py`         | SDCP protocol (ELEGOO / ChiTu)                                |
| `resinpilot/engine.py`       | Exposure recommendations                                      |
| `resinpilot/history.py`      | Print history (SQLite + `runs.jsonl` journal)                 |
| `resinpilot/web_service.py`  | Everything the web app asks for                               |
| `resinpilot/webapp.py`       | HTTP routes (FastAPI)                                         |
| `resinpilot/web/`            | Front end (plain HTML/CSS/JS, no build step)                  |
| `resinpilot/data/`           | Built-in printer models, resins and baseline exposures        |

## Adding a printer model

Add an entry to `resinpilot/data/printer_models.yaml`:

```yaml
- id: elegoo_saturn_3_ultra          # brand_model, lowercase
  brand: ELEGOO
  name: Saturn 3 Ultra
  driver: sdcp
  match_names:                       # what the printer reports as its machine name
  - Saturn 3 Ultra
  machine_type: ELEGOO Saturn 3 Ultra  # printer name used in sliced files
  machine_width: 218.88              # build plate, mm
  machine_depth: 122.88
  machine_height: 260.0
  resolution_x: 11520                # LCD pixels
  resolution_y: 5120
  project_type: LCD_mirror
  photo: assets/printers/elegoo_saturn_3_ultra.png   # optional, transparent PNG ~512 px
  specs:
  - 12K mono LCD · 11520 × 5120
```

To find `match_names`, add the printer in the app. The *Find printers* list shows the model name the printer reports.

Then add starting exposures for a few resins in `resinpilot/data/baselines.yaml` (`model_id` is the new model's `id`). Without baselines, the model borrows those of the library model with the closest pixel size.

## Adding a resin

Add it to `resinpilot/data/resins.yaml`, using the maker's recommended settings as `starting_exposure` and `starting_bottom_exposure`. Please include a source (the resin maker's settings page or data sheet) in your pull request.

## Writing a printer driver

A driver tells ResinPilot how to find printers of one protocol and how to read their live status. It is any class with these members:

```python
from resinpilot.drivers import FoundPrinter
from resinpilot.sdcp import PrinterLiveStatus


class MyBrandDriver:
    id = "mybrand"                 # stored in fleet.yaml; never change it once released
    label = "MyBrand (Wi-Fi)"      # shown in the app

    def discover(self, timeout_s: float = 2.5, target: str = "") -> list[FoundPrinter]:
        """Printers answering on the LAN, or only the one at `target` (an IP) when given."""
        return [
            FoundPrinter(driver=self.id, host="192.168.1.60", device_id="serial-123",
                         name="Workshop", model_name="Photon X", brand="MyBrand", firmware="1.0"),
        ]

    def describe(self, printer: FoundPrinter) -> dict:
        """Extra facts for models missing from the library:
        resolution_x/y and machine_width/depth/height. Return {} if unknown."""
        return {}

    def status(self, host: str, device_id: str) -> PrinterLiveStatus:
        """Live status. Return PrinterLiveStatus(connected=False, ip=host, raw_error=...) when unreachable."""
        ...
```

Fill in what your protocol offers on `PrinterLiveStatus`: `status_kind` (`printing`, `idle`, `busy`, `failed`, `offline`), progress, layers, file name and temperatures. ResinPilot polls `status()` every 20 s from a worker thread, so blocking I/O with a timeout is fine.

### Shipping a driver

Inside ResinPilot: put it in `resinpilot/drivers/` and register it in `pyproject.toml`:

```toml
[project.entry-points."resinpilot.drivers"]
mybrand = "resinpilot.drivers.mybrand:MyBrandDriver"
```

As a separate package (e.g. `resinpilot-mybrand`): add the same entry point to *your* package's `pyproject.toml`. Once it is installed next to ResinPilot it is picked up automatically. A driver that fails to load is skipped, so it can't break the app.

## Pull requests

- Run `pytest` before you push; add a test for new behaviour.
- Keep the front end dependency-free (no bundler).
- Never commit personal data: printer IPs, serial numbers, print history.
