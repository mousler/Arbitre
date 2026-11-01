#!/usr/bin/env python3
"""Install the official yt-dlp standalone executable for this platform."""

import json
import os
import platform
import shutil
import urllib.request


def asset_name(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Darwin":
        return "yt-dlp_macos"
    if system == "Windows":
        return "yt-dlp_arm64.exe" if machine in ("arm64", "aarch64") else "yt-dlp.exe"
    if system == "Linux":
        return "yt-dlp_linux_aarch64" if machine in ("arm64", "aarch64") else "yt-dlp_linux"
    raise RuntimeError(f"Plateforme non prise en charge : {system}")


def main():
    name = asset_name()
    request = urllib.request.Request(
        "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Rhetora"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((item for item in release["assets"] if item["name"] == name), None)
    if not asset:
        raise RuntimeError(f"Binaire {name} absent de la dernière version de yt-dlp")

    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
    os.makedirs(folder, exist_ok=True)
    destination = os.path.join(folder, name)
    temporary = destination + ".download"
    try:
        download = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "Rhetora"})
        with urllib.request.urlopen(download, timeout=120) as response, open(temporary, "wb") as output:
            shutil.copyfileobj(response, output)
        if os.name != "nt":
            os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    print(f"yt-dlp {release['tag_name']} installé : {destination}")


if __name__ == "__main__":
    main()