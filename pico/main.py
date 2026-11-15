# Stays on the Pico. Connects to WiFi, checks the GitHub version, updates if different, runs app.py.
import network, time, os, gc, machine
import config

try:
    import requests
except ImportError:
    import urequests as requests

BASE = "https://raw.githubusercontent.com/%s/%s/" % (config.REPO, config.BRANCH)


def read_local_version():
    try:
        with open("version.txt") as f:
            return f.read().strip()
    except OSError:
        return "0"


def wifi_connect(timeout=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        t = time.time()
        while not wlan.isconnected() and time.time() - t < timeout:
            time.sleep(0.5)
    return wlan.isconnected()


def get_bytes(path):
    r = requests.get(BASE + path)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, path))
        return r.content
    finally:
        r.close()
        gc.collect()


def update():
    remote = get_bytes("version.txt").decode().strip()
    local = read_local_version()
    if remote == local:
        return
    print("Updating", local, "->", remote)
    files = get_bytes("files.txt").decode().split()
    # download everything first, so a failed download changes nothing
    for name in files:
        data = get_bytes(name)
        with open(name + ".new", "wb") as f:
            f.write(data)
    # then swap files in
    for name in files:
        try:
            os.remove(name)
        except OSError:
            pass
        os.rename(name + ".new", name)
    with open("version.txt", "w") as f:
        f.write(remote)
    machine.reset()


if wifi_connect():
    try:
        update()
    except Exception as e:
        print("Update skipped:", e)

try:
    import app
    app.run()
except Exception as e:
    print("App crashed:", e)
    time.sleep(10)
    machine.reset()
