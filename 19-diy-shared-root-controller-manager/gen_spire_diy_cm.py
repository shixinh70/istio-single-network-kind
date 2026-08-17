import sys

# Usage: gen_spire_diy_cm.py <namespace> <cluster_name> <out_file>
#
# Combines two previously-separate, previously-verified pieces into one
# install:
#   - 18-diy-shared-root-nested-poc/: UpstreamAuthority "disk" in "join
#     existing PKI" mode (own intermediate + shared root), same
#     trust_domain across all clusters -> no bundle federation needed at
#     all (no bundle_set, no ClusterFederatedTrustDomain, no federatesWith).
#   - 17-spire-cross-cluster-mtls/ Phase 4: SPIRE Controller Manager
#     co-located as a second container in the spire-server StatefulSet,
#     driven by ClusterSPIFFEID CRs instead of manual `entry create`.
#
# Prerequisite (see gen_root_and_intermediates.sh): a Secret named
# `diy-intermediate` in this namespace containing intermediate.crt,
# intermediate.key, root.crt for THIS cluster specifically.

namespace = sys.argv[1]
cluster_name = sys.argv[2]
out_file = sys.argv[3]

trust_domain = "diy-shared.local"

doc = f"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: spire-server
  namespace: {namespace}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: spire-agent
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {namespace}-server-trust-role
rules:
- apiGroups: ["authentication.k8s.io"]
  resources: ["tokenreviews"]
  verbs: ["create"]
- apiGroups: [""]
  resources: ["pods", "nodes"]
  verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {namespace}-server-trust-role-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {namespace}-server-trust-role
subjects:
- kind: ServiceAccount
  name: spire-server
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {namespace}-agent-node-role
rules:
- apiGroups: [""]
  resources: ["pods", "nodes", "nodes/proxy"]
  verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {namespace}-agent-node-role-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {namespace}-agent-node-role
subjects:
- kind: ServiceAccount
  name: spire-agent
  namespace: {namespace}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: spire-server-conf
  namespace: {namespace}
data:
  server.conf: |
    server {{
      bind_address = "0.0.0.0"
      bind_port = "8081"
      trust_domain = "{trust_domain}"
      data_dir = "/run/spire/data"
      log_level = "INFO"
      ca_subject = {{
        country = ["US"],
        organization = ["spire-lab"],
        common_name = "{cluster_name}",
      }}
    }}
    plugins {{
      DataStore "sql" {{
        plugin_data {{
          database_type = "sqlite3"
          connection_string = "/run/spire/data/datastore.sqlite3"
        }}
      }}
      NodeAttestor "k8s_psat" {{
        plugin_data {{
          clusters = {{
            "{cluster_name}" = {{
              service_account_allow_list = ["{namespace}:spire-agent"]
            }}
          }}
        }}
      }}
      KeyManager "disk" {{
        plugin_data {{
          keys_path = "/run/spire/data/keys.json"
        }}
      }}
      UpstreamAuthority "disk" {{
        plugin_data {{
          cert_file_path = "/run/spire/diy-ca/intermediate.crt"
          key_file_path = "/run/spire/diy-ca/intermediate.key"
          bundle_file_path = "/run/spire/diy-ca/root.crt"
        }}
      }}
    }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: spire-controller-manager-config
  namespace: {namespace}
data:
  controller-manager-config.yaml: |
    apiVersion: spire.spiffe.io/v1alpha1
    kind: ControllerManagerConfig
    metadata:
      name: spire-controller-manager
      namespace: {namespace}
    health:
      healthProbeBindAddress: 0.0.0.0:8083
    leaderElection:
      leaderElect: true
      resourceName: spire-controller-manager-{cluster_name}.spiffe.io
      resourceNamespace: {namespace}
    clusterName: {cluster_name}
    trustDomain: {trust_domain}
    ignoreNamespaces:
      - kube-system
      - kube-public
      - local-path-storage
      - {namespace}
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
  namespace: {namespace}
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
  namespace: {namespace}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: spire-controller-manager-leader-election
subjects:
- kind: ServiceAccount
  name: spire-server
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: spire-controller-manager-{namespace}-{cluster_name}
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
  name: spire-controller-manager-{namespace}-{cluster_name}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: spire-controller-manager-{namespace}-{cluster_name}
subjects:
- kind: ServiceAccount
  name: spire-server
  namespace: {namespace}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: spire-server
  namespace: {namespace}
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
        - name: diy-ca
          mountPath: /run/spire/diy-ca
          readOnly: true
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
      - name: diy-ca
        secret:
          secretName: diy-intermediate
      - name: controller-manager-config
        configMap:
          name: spire-controller-manager-config
---
apiVersion: v1
kind: Service
metadata:
  name: spire-server
  namespace: {namespace}
spec:
  selector:
    app: spire-server
  ports:
  - port: 8081
    targetPort: 8081
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: spire-agent-conf
  namespace: {namespace}
data:
  agent.conf: |
    agent {{
      data_dir = "/run/spire"
      log_level = "INFO"
      server_address = "spire-server.{namespace}.svc.cluster.local"
      server_port = "8081"
      trust_domain = "{trust_domain}"
      socket_path = "/run/spire/sockets/socket"
      insecure_bootstrap = true
    }}
    plugins {{
      NodeAttestor "k8s_psat" {{
        plugin_data {{
          cluster = "{cluster_name}"
        }}
      }}
      KeyManager "memory" {{
        plugin_data {{}}
      }}
      WorkloadAttestor "k8s" {{
        plugin_data {{
          skip_kubelet_verification = true
        }}
      }}
    }}
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: spire-agent
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      app: spire-agent
  template:
    metadata:
      labels:
        app: spire-agent
    spec:
      hostPID: true
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      serviceAccountName: spire-agent
      containers:
      - name: spire-agent
        image: ghcr.io/spiffe/spire-agent:1.11.2
        args: ["-config", "/run/spire/config/agent.conf"]
        volumeMounts:
        - name: spire-config
          mountPath: /run/spire/config
          readOnly: true
        - name: spire-agent-socket
          mountPath: /run/spire/sockets
        - name: spire-token
          mountPath: /var/run/secrets/tokens
        securityContext:
          privileged: true
      volumes:
      - name: spire-config
        configMap:
          name: spire-agent-conf
      - name: spire-agent-socket
        # 刻意跟 17-/18- 用不同的 hostPath，三套並存互不干擾。
        # 在真正的目標環境（沒有其他 SPIRE 在跑）可以直接用
        # /run/spire/sockets，不用刻意分開。
        hostPath:
          path: /run/{namespace}/sockets
          type: DirectoryOrCreate
      - name: spire-token
        projected:
          sources:
          - serviceAccountToken:
              path: spire-agent
              expirationSeconds: 7200
              audience: spire-server
"""

with open(out_file, "w") as f:
    f.write(doc)

print(f"generated diy-shared-root + Controller Manager manifest for ns={namespace} cluster={cluster_name} trust_domain={trust_domain} -> {out_file}")
