import sys, os
# 把项目根目录加入 sys.path，让 tests/ 下的文件能直接 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
