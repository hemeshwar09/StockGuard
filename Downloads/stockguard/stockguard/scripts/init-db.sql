-- Runs once when the local Postgres container is first created.
-- The test suite uses its own database so it can drop/recreate tables freely.
CREATE DATABASE stockguard_test;
