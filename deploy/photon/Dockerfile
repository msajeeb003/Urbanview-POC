# Photon geocoder (https://github.com/komoot/photon) for the search box: OpenStreetMap addresses,
# streets, places and points of interest of one country, self-hosted so a typed address with its
# house number answers in milliseconds, with no fair-use limit and no 1-3 s round trip to the
# public instance (which the API had to drop at its 3 s timeout, so search said "No match").
#
# The database is Photon's own country dump, built weekly by GraphHopper from OpenStreetMap
# (https://download1.graphhopper.com/public/europe/montenegro/, about 5 MB for Montenegro):
# `entrypoint.sh` downloads and imports it on the first start (about a minute) and again when the
# imported copy is older than PHOTON_REIMPORT_DAYS. Java 21+ is Photon's requirement; the
# embedded OpenSearch keeps the index under /photon/data.
FROM eclipse-temurin:21-jre-jammy
ARG PHOTON_VERSION=1.3.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl zstd \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /photon
RUN curl -fsSL -o photon.jar \
      "https://github.com/komoot/photon/releases/download/${PHOTON_VERSION}/photon-${PHOTON_VERSION}.jar"
COPY entrypoint.sh /photon/entrypoint.sh
RUN chmod +x /photon/entrypoint.sh
VOLUME /photon/data
EXPOSE 2322
# the import runs before the server answers: the start period covers it
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s \
  CMD curl -fsS "http://127.0.0.1:2322/api?q=podgorica&limit=1" >/dev/null || exit 1
ENTRYPOINT ["/photon/entrypoint.sh"]
