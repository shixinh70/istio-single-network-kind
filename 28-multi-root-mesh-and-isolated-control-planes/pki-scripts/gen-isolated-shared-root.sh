#!/usr/bin/env bash
# Generates ONE shared root CA + a per-cluster intermediate for the
# "isolated" control-plane world (used so the two isolated revisions on
# cluster1 and cluster1-134 mutually trust each other from the start,
# with zero bundling step needed - see README section 4).
set -euo pipefail

OUT="${1:-./isolated-pki}"
mkdir -p "$OUT"
cd "$OUT"

cat <<'EOF' > minimal.cnf
[req]
distinguished_name = req_distinguished_name
prompt = no
[req_distinguished_name]
EOF

openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -nodes -key root.key -sha256 -days 3650 \
  -config minimal.cnf \
  -subj "/O=isolated-mesh/CN=Isolated Root CA" \
  -addext "basicConstraints=critical,CA:true" \
  -addext "keyUsage=critical,keyCertSign,cRLSign,digitalSignature" \
  -addext "subjectKeyIdentifier=hash" \
  -out root.crt

for cl in cluster1 cluster1-134; do
  openssl ecparam -name prime256v1 -genkey -noout -out "$cl.key"
  openssl req -new -key "$cl.key" -config minimal.cnf -subj "/O=isolated-mesh/CN=Intermediate-$cl" -out "$cl.csr"
  cat <<EXT > "$cl-ext.cnf"
basicConstraints=critical,CA:true,pathlen:0
keyUsage=critical,keyCertSign,cRLSign,digitalSignature
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always,issuer
EXT
  openssl x509 -req -in "$cl.csr" -CA root.crt -CAkey root.key -CAcreateserial -days 3650 -sha256 \
    -extfile "$cl-ext.cnf" -out "$cl.crt"
  cat "$cl.crt" root.crt > "$cl-chain.crt"
  openssl verify -CAfile root.crt "$cl.crt"
done
