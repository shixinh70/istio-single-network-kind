import sys

# Usage: gen_spire_diy.py <namespace> <cluster_label> <out_file>
# Throwaway SPIRE Server + Agent for this PoC only — deliberately isolated
# in its own namespace (spire-nested-a / spire-nested-b), separate hostPath
# socket dir, so it never touches the real `spire` namespace's verified
# Federation setup from 17-spire-cross-cluster-mtls/.
#
# Both instances (cluster1-134's spire-nested-a, cluster2-134's
# spire-nested-b) use the SAME trust_domain ("diy-shared.local") and each
# gets its OWN distinct intermediate CA (signed offline by a shared root,
# see gen_diy_pki.sh) via UpstreamAuthority "disk" in "join existing PKI"
# mode (cert_file_path=own intermediate, bundle_file_path=shared root).

namespace = sys.argv[1]
cluster_label = sys.argv[2]
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
        common_name = "{cluster_label}",
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
            "{namespace}" = {{
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
        - name: diy-ca
          mountPath: /run/spire/diy-ca
          readOnly: true
      volumes:
      - name: spire-config
        configMap:
          name: spire-server-conf
      - name: spire-data
        emptyDir: {{}}
      - name: diy-ca
        secret:
          secretName: diy-intermediate
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
          cluster = "{namespace}"
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
        # 刻意用跟正式 `spire` namespace 不同的 hostPath，避免衝突
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

print(f"generated DIY-nested SPIRE manifest for ns={namespace} cluster_label={cluster_label} trust_domain={trust_domain} -> {out_file}")
