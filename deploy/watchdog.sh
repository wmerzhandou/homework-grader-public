#!/bin/bash
# homework-grader 看门狗：健康检查连续失败 2 次则重启主服务。
# 覆盖两类故障：进程崩溃（Restart=always 已管）和进程卡死（只有外部探测能发现）。
FAILS=0
while true; do
  if curl -sk -m 10 -o /dev/null https://127.0.0.1:8040/api/health; then
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    if [ "$FAILS" -ge 2 ]; then
      logger -t homework-grader-watchdog "health check failed ${FAILS}x, restarting homework-grader.service"
      systemctl restart homework-grader.service
      FAILS=0
      sleep 60
    fi
  fi
  sleep 30
done
