"""Live overview in any terminal. Ctrl-C exits."""
import sys
from pathlib import Path
from MTS_Management.cli import main

if __name__ == "__main__":
    main(["--sites", str(Path(__file__).with_name("sites.toml")),
          "status", "overview", "--watch", *sys.argv[1:]])
