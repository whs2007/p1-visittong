# 开发与验收的单一入口。make 的每个目标都在 CI 里跑过一遍，避免「文档命令是错的」。
PY ?= python
BACKEND := backend
PYTEST := $(PY) -m pytest

.PHONY: help install run test lint eval eval-update smoke chaos backup verify reconcile clean-test-data

help:
	@echo "make install        安装锁定依赖（requirements.lock）"
	@echo "make run            本地 demo 模式启动（127.0.0.1:8020）"
	@echo "make test           全量单元/集成测试"
	@echo "make lint           ruff 静态检查"
	@echo "make eval           行为评测（与 baseline 比对，回归即失败）"
	@echo "make eval-update    更新评测基线（需在评审后执行）"
	@echo "make smoke          端到端 7 组冒烟"
	@echo "make chaos          真 kill -9 崩溃对账实验"
	@echo "make verify         lint + test + eval + smoke + chaos（发布门禁；Windows 无 make 时用 python backend/scripts/verify_all.py）"
	@echo "make backup         备份 SQLite 到 backend/data/backups"
	@echo "make reconcile      单独跑一次崩溃对账（多 worker 部署用）"

install:
	# lock 带 sha256 哈希：供应链被换包时直接装不上，而不是静默装上别的版本
	$(PY) -m pip install --require-hashes -r $(BACKEND)/requirements.lock

run:
	cd $(BACKEND) && AUTH_MODE=demo AUTH_ALLOW_INSECURE_SECRET=1 $(PY) run.py

test:
	$(PYTEST)

lint:
	$(PY) -m ruff check backend

eval:
	$(PY) $(BACKEND)/scripts/run_eval.py

eval-update:
	$(PY) $(BACKEND)/scripts/run_eval.py --update-baseline

smoke:
	# --serve：脚本自己拉一个隔离服务（独立端口 + 独立库），否则「一条命令验收」不成立
	$(PY) $(BACKEND)/scripts/smoke_scenarios.py --serve

chaos:
	$(PY) $(BACKEND)/scripts/chaos_resume.py

verify:
	# 门禁顺序与 CI 都在 backend/scripts/verify_all.py 里，避免两套定义漂移
	$(PY) $(BACKEND)/scripts/verify_all.py

reconcile:
	# 多 worker / 多副本时由它单独跑一次对账（web 进程设 RECONCILE_ON_START=0）
	cd $(BACKEND) && $(PY) -m app.maintenance reconcile

backup:
	$(PY) $(BACKEND)/scripts/backup.py

# 本地跑测试会累积 backend/data/_pytest（为绕开宿主的批量删除保护而刻意不自动清理）。
# CI 里由 workflow 的 post-job 清理，本地需要时手动执行这一条。
clean-test-data:
	$(PY) $(BACKEND)/scripts/clean_test_data.py
