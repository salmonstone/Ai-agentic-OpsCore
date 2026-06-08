import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_MAIN_FREE", "1")
os.environ.setdefault("GOTO_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from agent.cli import main

if __name__ == "__main__":
    main()
