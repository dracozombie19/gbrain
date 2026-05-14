import postgres from 'postgres';

// Connect as postgres (superuser) to grant BYPASSRLS and full privileges to gbrain user.
// Reads the admin connection string from POSTGRES_ADMIN_URL so credentials are never
// committed. Example (PowerShell):
//   $env:POSTGRES_ADMIN_URL = "postgres://postgres:PASSWORD@HOST:5432/gbrain?sslmode=require"
//   bun run scripts/grant-privileges.ts
//   Remove-Item Env:POSTGRES_ADMIN_URL
const adminUrl = process.env.POSTGRES_ADMIN_URL;
if (!adminUrl) {
  console.error('Error: POSTGRES_ADMIN_URL env var is required.');
  console.error('Set it to the postgres-role connection string before running this script.');
  process.exit(1);
}

const sql = postgres(adminUrl, {
  ssl: 'require',
  max: 1,
  connect_timeout: 30,
});

console.log('Connecting as postgres superuser...');

try {
  // Grant BYPASSRLS so migrations with RLS succeed
  await sql`ALTER USER gbrain WITH BYPASSRLS`;
  console.log('✅ Granted BYPASSRLS to gbrain');

  // Grant all existing table/sequence privileges
  await sql`GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO gbrain`;
  await sql`GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO gbrain`;
  await sql`GRANT ALL PRIVILEGES ON SCHEMA public TO gbrain`;
  console.log('✅ Granted ALL PRIVILEGES on existing objects');

  // Make future objects also accessible
  await sql`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO gbrain`;
  await sql`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO gbrain`;
  console.log('✅ Set default privileges for future objects');

  console.log('\n✅ All done! The gbrain user now has full access including BYPASSRLS.');
} catch (err) {
  console.error('Error:', err);
  process.exit(1);
} finally {
  await sql.end();
}
