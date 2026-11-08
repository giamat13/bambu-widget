# Bambu Widget

A small, borderless, always-on-top desktop widget that shows the progress of your Bambu Lab 3D printer.

## Requirements

- Python 3.9+
- [paho-mqtt](https://pypi.org/project/paho-mqtt/)

```bash
pip install paho-mqtt
```

## Usage

**Cloud mode** (printer bound to your Bambu account, no LAN Only Mode needed):

```bash
python main.py --login                       # one-time: prints uid, token and serial
python main.py --cloud --uid 123456 --token <TOKEN> --serial 01P00A000000000
```

**Local mode** (printer needs LAN Only Mode and Developer Mode):

```bash
python main.py --ip 192.168.1.50 --serial 01P00A000000000 --code 12345678
```

**Demo** (no printer needed):

```bash
python main.py --demo
```

Environment variables `BAMBU_IP`, `BAMBU_SERIAL`, `BAMBU_CODE`, `BAMBU_UID` and `BAMBU_TOKEN` can be used instead of command-line flags.

Keep your token and printer access code private. Pass them through environment variables set in your shell rather than writing them into files you commit.

## Controls

- Drag with the left mouse button to move the window.
- Right-click for the menu (compact view / quit).

## License

MIT. See [LICENSE](LICENSE).
