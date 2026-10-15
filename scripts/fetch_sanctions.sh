#!/usr/bin/env bash
# Download public designation lists into data/sanctions (or $1). Run daily from cron/CI, then restart the service.
# NOTE: written without network access to these hosts; verify URLs against the publishers' current pages:
#   OFAC SDN:   https://ofac.treasury.gov/sanctions-list-service
#   UN list:    https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list
set -euo pipefail
dir="${1:-data/sanctions}"; mkdir -p "$dir"
curl -fsSL --retry 3 -o "$dir/sdn.csv.tmp" https://www.treasury.gov/ofac/downloads/sdn.csv && mv "$dir/sdn.csv.tmp" "$dir/sdn.csv"
curl -fsSL --retry 3 -o "$dir/alt.csv.tmp" https://www.treasury.gov/ofac/downloads/alt.csv && mv "$dir/alt.csv.tmp" "$dir/alt.csv"
curl -fsSL --retry 3 -o "$dir/consolidated.xml.tmp" https://scsanctions.un.org/resources/xml/en/consolidated.xml && mv "$dir/consolidated.xml.tmp" "$dir/consolidated.xml"
echo "OK: $(wc -l < "$dir/sdn.csv") SDN rows; $(wc -c < "$dir/consolidated.xml") bytes UN XML"
