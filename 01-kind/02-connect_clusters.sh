#!/bin/bash

# 檢查參數
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <cluster1-name> <cluster2-name>"
  exit 1
fi

CLUSTER1="$1"        # 例如: a
CLUSTER2="$2"        # 例如: b
CONTEXT1="$CLUSTER1"
CONTEXT2="$CLUSTER2"

# 取得某個 context 的 IP routes
get_routes() {
  local context=$1
  kubectl --context "$context" get nodes -o=jsonpath='{range .items[*]}{"ip route add "}{.spec.podCIDR}{" via "}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}'
}

# 對指定 cluster 的每個 node container 執行 ip route add
apply_routes_to_cluster() {
  local cluster_name=$1
  local routes=$2

  local nodes=$(kind get nodes --name "$cluster_name")

  for node in $nodes; do
    echo "Applying routes to node: $node"
    while IFS= read -r route; do
      echo "  $route"
      docker exec "$node" bash -c "$route"
    done <<< "$routes"
  done
}

# 取得 cluster1 的路由，加入 cluster2
routes_1=$(get_routes "$CONTEXT1")
apply_routes_to_cluster "$CLUSTER2" "$routes_1"

# 取得 cluster2 的路由，加入 cluster1
routes_2=$(get_routes "$CONTEXT2")
apply_routes_to_cluster "$CLUSTER1" "$routes_2"
