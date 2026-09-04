#!/usr/bin/env bash
# Generates two INDEPENDENT root CAs (one per cluster) for testing the
# "different root CA per cluster" baseline scenario, plus the bundle file
# used to establish mutual trust afterwards. Run from an empty output dir.
set -euo pipefail

OUT="${1:-./fresh-roots}"
mkdir -p "$OUT/cluster1" "$OUT/cluster1-134"

cat <<'EOF' > "$OUT/minimal.cnf"
[req]
distinguished_name = req_distinguished_name
prompt = no
[req_distinguished_name]
EOF

gen_root_and_intermediate() {
  local dir="$1" org="$2" cn_root="$3"
  cd "$dir"
  openssl ecparam -name prime256v1 -genkey -noout -out root-key.pem
  openssl req -x509 -new -nodes -key root-key.pem -sha256 -days 3650 \
    -config ../minimal.cnf \
    -subj "/O=${org}/CN=${cn_root}" \
    -addext "basicConstraints=critical,CA:true" \
    -addext "keyUsage=critical,keyCertSign,cRLSign,digitalSignature" \
    -addext "subjectKeyIdentifier=hash" \
    -out root-cert.pem

  openssl ecparam -name prime256v1 -genkey -noout -out ca-key.pem
  openssl req -new -key ca-key.pem -config ../minimal.cnf -subj "/O=${org}/CN=Intermediate CA" -out ca.csr
  cat <<EXT > int-ext.cnf
basicConstraints=critical,CA:true,pathlen:0
keyUsage=critical,keyCertSign,cRLSign,digitalSignature
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always,issuer
EXT
  openssl x509 -req -in ca.csr -CA root-cert.pem -CAkey root-key.pem -CAcreateserial -days 3650 -sha256 \
    -extfile int-ext.cnf -out ca-cert.pem
  cat ca-cert.pem root-cert.pem > cert-chain.pem
  openssl verify -CAfile root-cert.pem ca-cert.pem
  cd - >/dev/null
}

gen_root_and_intermediate "$OUT/cluster1" "cluster1-fresh-pki" "cluster1 Root CA"
gen_root_and_intermediate "$OUT/cluster1-134" "cluster1-134-fresh-pki" "cluster1-134 Root CA"

# Bundle: both roots concatenated - this is what "multi-root" trust means in
# practice, see README section 1 for why this works and why the official
# ISTIO_MULTIROOT_MESH / meshConfig.caCertificates mechanism did NOT work on
# 1.13.5 despite being the "textbook" API for this.
cat "$OUT/cluster1/root-cert.pem" "$OUT/cluster1-134/root-cert.pem" > "$OUT/bundle-root.pem"
echo "bundle has $(grep -c 'BEGIN CERTIFICATE' "$OUT/bundle-root.pem") certs"
