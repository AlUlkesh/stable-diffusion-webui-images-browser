import launch
import os

if not launch.is_installed("send2trash"):
    launch.run_pip("install Send2Trash", "Send2Trash requirement for image browser")

try:
    import ImageReward
    import dataset
    import dill
    import diffusers
    import multiprocessing
    import pyarrow
    import xxhash
except ImportError as e:
    req_IR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "req_IR.txt")
    launch.run_pip(f'install -r "{req_IR}" --no-deps image-reward', 'ImageReward requirement for image browser')
