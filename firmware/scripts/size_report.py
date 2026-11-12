"""Post-build size report: the ESP8684 has ~272 KB SRAM and (usually) 2 MB flash.

Prints the binary footprint so regressions are visible in the build log instead
of discovered on hardware. Runs as a PlatformIO `post` script.
"""

Import("env")  # noqa: F821  (PlatformIO injects this)

def _size_report(source, target, env):  # noqa: ARG001
    try:
        import subprocess

        elf = env.subst("$BUILD_DIR/${PROGNAME}.elf")
        size_tool = env.subst("$OBJCOPY").replace("objcopy", "size")
        out = subprocess.run([size_tool, "-A", elf], capture_output=True, text=True)
        if out.returncode == 0:
            lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
            print("\n=== firmware size ===")
            for ln in lines[-8:]:
                print(ln)
            print("=====================\n")
    except Exception as exc:  # never fail the build over a report
        print(f"[size_report] skipped: {exc}")


env.AddPostAction("$BUILD_DIR/${PROGNAME}.elf", _size_report)  # noqa: F821
