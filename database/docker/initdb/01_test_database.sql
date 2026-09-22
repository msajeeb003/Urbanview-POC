-- Runs once when the compose postgres volume is first created, after the postgis image's own
-- init script. Creates the database used by `make test-integration`.
CREATE DATABASE urbanview_test;
\connect urbanview_test
CREATE EXTENSION IF NOT EXISTS postgis;
