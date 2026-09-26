#!/bin/bash

# PostgreSQL initialization script for warm storage
# This script creates the warm_store database and runs the DDL
# It waits for PostgreSQL to be ready before executing

set -e

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
until pg_isready -h localhost -p 5432 -U warm; do
    echo "PostgreSQL is unavailable - sleeping"
    sleep 2
done

echo "PostgreSQL is ready"

# Create the warm_store database
echo "Creating warm_store database..."
createdb -h localhost -p 5432 -U warm warm_store

# Create warm user with limited privileges
echo "Creating warm user..."
psql -h localhost -p 5432 -U postgres -c "
CREATE USER IF NOT EXISTS warm_user 
WITH PASSWORD 'warm_password'
"

# Grant privileges to warm_user on warm_store database
echo "Granting privileges to warm_user..."
psql -h localhost -p 5432 -U postgres -c "
GRANT ALL PRIVILEGES ON DATABASE warm_store TO warm_user
"

# Grant usage on schema
psql -h localhost -p 5432 -U warm -d warm_store -c "
GRANT ALL ON ALL TABLES IN SCHEMA public TO warm_user
"
psql -h localhost -p 5432 -U warm -d warm_store -c "
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO warm_user
"
psql -h localhost -p 5432 -U warm -d warm_store -c "
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO warm_user
"

# Run the DDL script
echo "Running DDL script..."
psql -h localhost -p 5432 -U warm -d warm_store -f /docker-entrypoint-initdb.d/postgres_warm.sql

# Verify tables were created
echo "Verifying tables..."
psql -h localhost -p 5432 -U warm -d warm_store -c "
SELECT 
    tablename,
    tableowner,
    hasindexes,
    hasrules,
    hastriggers
FROM pg_tables 
WHERE schemaname = 'public'
ORDER BY tablename
"

echo "PostgreSQL warm storage initialization completed successfully"