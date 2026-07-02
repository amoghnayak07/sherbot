setup:
	k3d cluster create sherbot --api-port 6443 --image rancher/k3s:v1.31.4-k3s1
	k3d kubeconfig merge sherbot --kubeconfig-merge-default
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/rbac.yaml
	kubectl apply -f k8s/clickhouse.yaml
	kubectl apply -f k8s/otel-collector-config.yaml
	kubectl apply -f k8s/otel-collector.yaml

backend:
	docker build -t rca-backend:latest ./services/backend
	k3d image import rca-backend:latest -c sherbot
	kubectl apply -f services/backend/k8s/

teardown:
	k3d cluster delete sherbot
