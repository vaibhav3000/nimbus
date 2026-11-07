pkill -f ign
pkill -f ros
# kill all the processes which were returned by the ps aux | grep run_supervisor.py command
ps aux | grep run_supervisor.py | grep -v grep | awk '{print $2}' | xargs kill -9
