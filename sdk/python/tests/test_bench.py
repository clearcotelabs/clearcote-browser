"""`clearcote bench` and the Host section of `clearcote info`: the summary and the printed lines are a
contract shared with the Node SDK (test/bench.test.ts reads the same fixture and expected lines)."""
import json
import os

from clearcote import _commands
from clearcote._bench import (_secs, bench_lines, browser_tree, host_info, host_lines, proxy_label, ram_safe_count,
                              summarize_bench, windows_name)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "data", "bench_raw.json"), encoding="utf-8") as f:
    RAW = json.load(f)
with open(os.path.join(HERE, "data", "bench_lines.txt"), encoding="utf-8") as f:
    EXPECTED = f.read().replace("\r\n", "\n").rstrip("\n").split("\n")


def host(**over):
    h = {"cpu": {"model": "AMD Ryzen 7 5700G", "threads": 16}, "memory": {"totalMB": 16180, "freeMB": 4096},
         "os": "Microsoft Windows 11 Pro (10.0.26200)", "virtual": None, "remoteSession": False,
         "gpu": {"adapters": ["NVIDIA GeForce RTX 3070"], "software": False}}
    h.update(over)
    return h


def test_prints_the_shared_fixture_lines_exactly():
    assert bench_lines(summarize_bench(RAW)) == EXPECTED


def test_measures_per_browser_from_the_browsers_that_started():
    r = summarize_bench(RAW)
    assert r["launch"] == {"started": 4, "medianMs": 5200,
                           "errors": ["browserType.launchPersistentContext: licence: all 4 seats are in use"]}
    assert r["perBrowser"] == {"cpuThreads": 1.4, "cpuPercentOfMachine": 17.7, "memMB": 611}
    assert r["capacity"] == {"browsers": 3, "limit": "ram", "byCpu": 4, "byRam": 3}
    assert r["machine"] == {"cpuBefore": 61, "cpuDuring": 100, "saturated": True}


def test_json_report_matches_node_shape():
    # Same keys in the same order as the Node SDK's BenchReport.
    assert list(summarize_bench(RAW)) == ["host", "url", "proxy", "headless", "requested", "concurrency",
                                          "durationSec", "launch", "pages", "perBrowser", "machine", "capacity",
                                          "advice"]


def test_blames_the_browsers_only_when_they_caused_most_of_the_rise():
    assert summarize_bench(RAW)["advice"][0].startswith("The CPU was full during the test: 5 browsers at once")
    idle = dict(RAW, browsers=[dict(b, cpuMs=b["cpuMs"] / 10) for b in RAW["browsers"]])
    assert "these browsers used only 7.2 % of it: other programs" in summarize_bench(idle)["advice"][0]
    assert summarize_bench(dict(RAW, machineDuring=70))["machine"]["saturated"] is False


def test_rounds_ties_like_the_node_sdk():
    # Python's format gives "0.2" for 0.25, JS toFixed "0.3": both SDKs round half-up first.
    assert _secs(250) == "0.3 s"
    assert _secs(1250) == "1.3 s"
    assert _secs(None) == "-"


def test_caps_by_free_ram():
    assert ram_safe_count(1386) == 2
    assert ram_safe_count(100) == 1
    assert ram_safe_count(16000) == 30


def test_never_prints_proxy_credentials():
    assert proxy_label("http://user:pa%40ss@gw.example:7777") == "http://gw.example:7777"
    assert proxy_label("socks5://gw.example:1080") == "socks5://gw.example:1080"


def test_browser_tree_follows_the_profile_dir():
    d = "C:\\Temp\\clearcote-bench-abc"
    table = [
        {"pid": 1, "ppid": 0, "cmd": f"chrome.exe --user-data-dir={d} --headless", "cpuMs": 10, "memMB": 100},
        {"pid": 2, "ppid": 1, "cmd": "chrome.exe --type=renderer", "cpuMs": 20, "memMB": 200},
        {"pid": 3, "ppid": 2, "cmd": "chrome.exe --type=utility", "cpuMs": 1, "memMB": 5},
        {"pid": 4, "ppid": 1, "cmd": f"chrome.exe --type=gpu-process --user-data-dir={d}", "cpuMs": 5, "memMB": 50},
        {"pid": 9, "ppid": 0, "cmd": "chrome.exe --user-data-dir=C:\\Other", "cpuMs": 99, "memMB": 999},
        {"pid": 10, "ppid": 9, "cmd": "chrome.exe --type=renderer", "cpuMs": 99, "memMB": 999},
    ]
    assert sorted(browser_tree(table, d)) == [1, 2, 3, 4]


def test_host_lines():
    assert host_lines(host()) == [
        "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
        "                Microsoft Windows 11 Pro (10.0.26200), physical machine",
        "GPU             NVIDIA GeForce RTX 3070",
    ]
    assert host_lines(host(virtual="container", gpu={"adapters": [], "software": True})) == [
        "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
        "                Microsoft Windows 11 Pro (10.0.26200), container",
        "GPU             none found: Chromium draws every page on the CPU",
    ]
    assert host_lines(host(gpu={"adapters": ["ASPEED Graphics Family"], "software": True}))[2] == (
        "GPU             ASPEED Graphics Family (no 3D acceleration: Chromium draws every page on the CPU)")


def test_host_is_read_once_per_process_and_handed_out_as_copies():
    a = host_info()
    a["gpu"]["adapters"].append("changed by the caller")
    a["cpu"]["model"] = "changed"
    b = host_info()
    assert b["cpu"]["model"] != "changed"
    assert "changed by the caller" not in b["gpu"]["adapters"]
    assert b["memory"]["totalMB"] == a["memory"]["totalMB"]
    assert b["memory"]["freeMB"] > 0


def test_names_windows_11_although_its_product_name_says_10():
    assert windows_name("Windows 10 Pro", "10", "0", "26200") == "Windows 11 Pro (10.0.26200)"
    assert windows_name("Windows 10 Home", "10", "0", "19045") == "Windows 10 Home (10.0.19045)"
    assert windows_name("Windows Server 2022 Datacenter", "10", "0", "20348") == "Windows Server 2022 Datacenter (10.0.20348)"
    assert windows_name("", "", "", "") == "Windows (10.0.?)"


def test_documented_with_its_flags():
    u = _commands.usage()
    for s in ("clearcote bench", "BENCH FLAGS", "--concurrency", "--duration"):
        assert s in u
    assert "--force" in _commands.command_usage("bench")
    assert "seats" in _commands.command_usage("bench")


def test_bench_flags_match_node():
    node_src = os.path.join(HERE, "..", "..", "node", "src", "cli-commands.ts")
    if not os.path.exists(node_src):  # an sdist without the Node tree
        return
    with open(node_src, encoding="utf-8") as f:
        src = f.read()
    u = _commands.usage()

    def section(text):
        start = text.index("BENCH FLAGS")
        return text[start:text.index("ENVIRONMENT", start)]

    assert section(u) == section(src)


def test_bench_rejects_a_bad_concurrency(capsys):
    assert _commands.main(["bench", "--concurrency", "0"]) == 2
    assert "--concurrency must be a whole number of at least 1" in capsys.readouterr().err
