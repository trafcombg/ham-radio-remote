# redist/

Third-party installers referenced by `../server.iss` and `../client.iss`,
not bundled in this repo (licensing/size — fetch them yourself):

- `com0com-setup.exe` — virtual COM port driver, used by the client
  installer. Get a signed build from https://com0com.sourceforge.net or
  a maintained fork. Verify the silent-install flag (`/S` is what the
  classic NSIS-based installer uses — an MSI-based fork would need
  `msiexec /i com0com.msi /quiet` instead; `client.iss` assumes the
  former).
- `postgresql-setup.exe` — only needed if you want the server
  installer's optional "Install PostgreSQL" task to work. Get it from
  https://www.postgresql.org/download/windows/. Verify the unattended
  flags in `server.iss` against the actual version you're bundling.

Both `[Files]` entries use `skipifsourcedoesntexist`, so the installers
compile fine without these present — the corresponding `[Run]` step is
simply skipped, and the user installs that driver/database themselves.
