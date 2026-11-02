# Environment Setup

Tested on Ubuntu (UTM on Mac) with system Python 3.14. Ryu runs in a Python 3.11 venv.

## 1. Mininet and Open vSwitch

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch git
sudo mn --test pingall   # expect: Results: 0% dropped
```

## 2. Python 3.11 (skip if your Ubuntu already has it)

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

## 3. Ryu in a virtual environment

```bash
python3.11 -m venv ~/sdn_env
source ~/sdn_env/bin/activate

# pin old setuptools before installing Ryu (Ryu's build needs it)
pip install "setuptools==58.2.0" wheel pbr

# install Ryu and eventlet together, bypassing pip's build sandbox
pip install --no-build-isolation ryu eventlet==0.30.2

# upgrade libraries that break on Python 3.10+
pip install -U eventlet dnspython

# patch Ryu's web interface module (only the REST API uses this; we don't)
sed -i 's/from eventlet.wsgi import ALREADY_HANDLED/ALREADY_HANDLED = []/' \
    ~/sdn_env/lib/python3.11/site-packages/ryu/app/wsgi.py

# verify
ryu-manager --version   # should print: ryu-manager 4.34
```

## 4. matplotlib (optional, needed for the benchmark chart)

```bash
sudo apt install -y python3-matplotlib
```

## 5. Daily workflow

```bash
# Always in this order — mn -c kills ryu-manager if it is running
sudo mn -c

# Terminal 1: start controller
source ~/sdn_env/bin/activate
ryu-manager sdn_controller.py

# Terminal 2: start network (system Python, NOT inside the venv)
sudo python3 topology.py
```

## 6. Clone the repo into the VM

```bash
# Replace YOUR_USERNAME with your GitHub username
git clone https://github.com/YOUR_USERNAME/project-29-sdn-task-system.git
cd project-29-sdn-task-system
```
