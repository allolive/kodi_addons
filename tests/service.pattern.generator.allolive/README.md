# service.pattern.generator.allolive tests

    pytest tests/service.pattern.generator.allolive            # from the repository root (~4 min)
    pytest tests/service.pattern.generator.allolive -k g1      # one test

Runs against the add-on in `../../addons/service.pattern.generator.allolive` (`PGADDON` overrides
it). Needs python3, pytest, ffmpeg/ffprobe and perl; the ffmpeg checks are skipped
without ffmpeg. Each test runs in its own temporary directory on its own ports.

- `test_pattern_generator.py` - the pytest suite: every `t_*.py` check script as one test,
  plus the bit-exact HEVC, clip, Dolby Vision RPU and protocol checks.
- `pgtest.py` / `conftest.py` - how the scripts are run (directory, ports, timeouts).
- `t_*.py` - the check scripts (each exits 1 on a failure, and can be run on its own);
  `testlib.py` - what they share (check/FAILS, ports, xfer, an ST 2084 oracle, a recording
  backend, the rendered-scene capture); `fake_kodi.py` - Kodi's modules for the scripts that
  load kodi.py or default.py (`t_kodi.py` has its own, which simulates the player);
  `classic_rig.py` / `status_rig.py` - the rigs built on them; `calman_sim.py` drives a
  generator like Calman (UPGCI, port 2100).
- `apt-packages.txt` - the system packages CI installs for these tests; `setup.sh` fetches
  the rest (dovi_tool) and is run by CI too - run it once before testing locally.
- `dvref/` - reference Dolby Vision RPUs (from `dovi_tool generate`).
- `dtbin/` - dovi_tool (x86_64, MIT; fetched by `setup.sh`, git-ignored), which checks
  the Dolby Vision metadata the clips and streams carry; that test is skipped without it.
- `pgenerator-ref/` - PGenerator's own Perl modules and config (GPL, see COPYING), run
  side by side with the add-on by `t_classic2.py`.

Ports come from a random PGPORT below 32768, where Linux starts giving ports to
outgoing connections, and each script takes its own range of it; a port another program
holds is skipped.