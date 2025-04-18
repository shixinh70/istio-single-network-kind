#!/bin/bash

# 設定預設 cluster 名稱
DEFAULT_CLUSTER1="cluster1"
DEFAULT_CLUSTER2="cluster2"

# 如果沒有輸入參數，使用預設值
CLUSTER1="${1:-$DEFAULT_CLUSTER1}"
CLUSTER2="${2:-$DEFAULT_CLUSTER2}"

CONTEXT1="$CLUSTER1"
CONTEXT2="$CLUSTER2"

echo "🔗 Connecting clusters: $CLUSTER1 <--> $CLUSTER2"

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
