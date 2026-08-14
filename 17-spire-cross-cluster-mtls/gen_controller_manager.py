import sys

# Usage: gen_controller_manager.py <cluster_name> <trust_domain> <out_file>
# Adds spire-controller-manager as a sidecar to the EXISTING spire-server
# StatefulSet (same pattern the official helm-charts-hardened chart uses:
# one pod, shared local admin socket at /tmp/spire-server/private/api.sock,
# no network hop). Patches the StatefulSet in place via strategic merge
# (kubectl apply on top of gen_spire_cluster.py's StatefulSet) rather than
# regenerating the whole thing, so the existing server.conf/data volume
# setup from Phase 1 is untouched.

cluster_name = sys.argv[1]
trust_domain = sys.argv[2]
out_file = sys.argv[3]

doc = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: spire-controller-manager-config
  namespace: spire
data:
  controller-manager-config.yaml: |
    apiVersion: spire.spiffe.io/v1alpha1
    kind: ControllerManagerConfig
    metadata:
      name: spire-controller-manager
      namespace: spire
    health:
      healthProbeBindAddress: 0.0.0.0:8083
    leaderElection:
      leaderElect: true
      resourceName: spire-controller-manager-{cluster_name}.spiffe.io
      resourceNamespace: spire
    clusterName: {cluster_name}
    trustDomain: {trust_domain}
    ignoreNamespaces:
      - kube-system
      - kube-public
      - local-path-storage
      - spire
    spireServerSocketPath: "/tmp/spire-server/private/api.sock"
    className: ""
    watchClassless: true
    parentIDTemplate: "spiffe://{{{{ .TrustDomain }}}}/spire/agent/k8s_psat/{{{{ .ClusterName }}}}/{{{{ .NodeMeta.UID }}}}"
    reconcile:
      clusterSPIFFEIDs: true
      clusterStaticEntries: false
      clusterFederatedTrustDomains: false
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: spire-controller-manager-leader-election
  namespace: spire
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: [""]
  resources: ["events"]
  verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: spire-controller-manager-leader-election
  namespace: spire
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: spire-controller-manager-leader-election
subjects:
- kind: ServiceAccount
  name: spire-server
  namespace: spire
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: spire-controller-manager-{cluster_name}
rules:
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["endpoints"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["spire.spiffe.io"]
  resources: ["clusterfederatedtrustdomains", "clusterspiffeids", "clusterstaticentries"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["spire.spiffe.io"]
  resources: ["clusterfederatedtrustdomains/finalizers", "clusterspiffeids/finalizers", "clusterstaticentries/finalizers"]
  verbs: ["update"]
- apiGroups: ["spire.spiffe.io"]
  resources: ["clusterfederatedtrustdomains/status", "clusterspiffeids/status", "clusterstaticentries/status"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: spire-controller-manager-{cluster_name}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: spire-controller-manager-{cluster_name}
subjects:
- kind: ServiceAccount
  name: spire-server
  namespace: spire
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: spire-server
  namespace: spire
spec:
  serviceName: spire-server
  replicas: 1
  selector:
    matchLabels:
      app: spire-server
  template:
    metadata:
      labels:
        app: spire-server
    spec:
      serviceAccountName: spire-server
      containers:
      - name: spire-server
        image: ghcr.io/spiffe/spire-server:1.11.2
        args: ["-config", "/run/spire/config/server.conf"]
        ports:
        - containerPort: 8081
        volumeMounts:
        - name: spire-config
          mountPath: /run/spire/config
          readOnly: true
        - name: spire-data
          mountPath: /run/spire/data
        - name: spire-server-socket
          mountPath: /tmp/spire-server/private
      - name: spire-controller-manager
        image: ghcr.io/spiffe/spire-controller-manager:0.7.0
        args: ["--config=/controller-manager-config.yaml"]
        env:
        - name: ENABLE_WEBHOOKS
          value: "false"
        volumeMounts:
        - name: spire-server-socket
          mountPath: /tmp/spire-server/private
          readOnly: true
        - name: controller-manager-config
          mountPath: /controller-manager-config.yaml
          subPath: controller-manager-config.yaml
          readOnly: true
      volumes:
      - name: spire-config
        configMap:
          name: spire-server-conf
      - name: spire-data
        emptyDir: {{}}
      - name: spire-server-socket
        emptyDir: {{}}
      - name: controller-manager-config
        configMap:
          name: spire-controller-manager-config
"""

with open(out_file, "w") as f:
    f.write(doc)

print(f"generated controller-manager addition for cluster={cluster_name} -> {out_file}")
