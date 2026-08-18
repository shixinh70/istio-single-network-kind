import sys

# Usage: gen_spire_1152.py <namespace> <cluster_name> <out_file>
#
# Same architecture as 19-diy-shared-root-controller-manager/ (DIY shared
# root via UpstreamAuthority "disk" join-PKI mode + ClusterSPIFFEID/
# Controller Manager), bumped to SPIRE 1.15.2 (latest, vs. 1.11.2 used
# everywhere else in this repo) and targeted at cluster1/cluster2 —
# both running Istio 1.13.5 — to test whether the latest SPIRE release
# has any compatibility issue with an old Istio version, and to confirm
# istiod's own CA stays unaffected. Isolated in its own `spire-1315`
# namespace so it doesn't touch cluster2's existing `spire` namespace
# (19-'s live, verified install).
#
# Prerequisite: a Secret named `diy-intermediate` in this namespace
# containing intermediate.crt, intermediate.key, root.crt for THIS
# cluster specifically.

namespace = sys.argv[1]
cluster_name = sys.argv[2]
out_file = sys.argv[3]

trust_domain = "diy-1152.local"
spire_version = "1.15.2"
controller_manager_version = "0.7.0"

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
        image: ghcr.io/spiffe/spire-server:{spire_version}
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
        image: ghcr.io/spiffe/spire-controller-manager:{controller_manager_version}
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
        image: ghcr.io/spiffe/spire-agent:{spire_version}
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

print(f"generated SPIRE {spire_version} manifest for ns={namespace} cluster={cluster_name} trust_domain={trust_domain} -> {out_file}")
