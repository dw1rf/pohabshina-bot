import socket
import urllib.request

hosts = [
    "api.themoviedb.org",
    "api.watchmode.com",
    "www.omdbapi.com",
]

urls = [
    "https://api.themoviedb.org/3/configuration",
    "https://api.watchmode.com/",
    "https://www.omdbapi.com/",
]

for host in hosts:
    try:
        print(f"{host} -> {socket.gethostbyname_ex(host)}")
    except Exception as e:
        print(f"{host} DNS ERROR: {e}")

for url in urls:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            print(f"{url} -> HTTP {r.status}")
    except Exception as e:
        print(f"{url} ERROR: {e}")