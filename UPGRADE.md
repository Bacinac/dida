# Upgrading DIDA

```sh
cd /mnt/docker/dida
./deploy/upgrade.sh
```

That is the whole procedure. It backs the database up, fetches, rebuilds, restarts,
waits for every container to report healthy, and puts the previous stack back if
they don't.

**Do not upgrade by hand.** `git pull && docker compose build <service>` looks
equivalent and is not: `dida_core` is baked into the `dida/base` image and every
service image is `FROM` it, so building a service without rebuilding the base first
gives you new service code running against yesterday's core. Best case that is an
`ImportError` at boot; worst case it starts and behaves subtly differently. The
script always builds the base first, unconditionally.

---

## What the script does, in order

1. **Backs up Postgres** — `pg_dump -Fc` of the whole database, before anything else
   can change, and verifies the archive is readable with `pg_restore --list`. A
   failed or unreadable dump stops the upgrade. The newest 5 are kept
   (`DIDA_UPGRADE_KEEP` to change that) under `$DIDA_BACKUP_HOST/pre-upgrade/`, or
   `./.backups/pre-upgrade/` if that is not set.
2. **Fetches** with a bounded timeout, then records the commit you are on now.
3. **Checks whether this upgrade is reversible** — see below.
4. **Tags the running images** `:rollback`, and refuses to continue if it cannot
   snapshot any of them.
5. **Builds** the base image, then every service.
6. **Starts** and waits up to 3 minutes for every container to be healthy.
7. On failure, **rolls back** — or refuses to, and says why.

## The two ways an upgrade can end badly

**Health does not come, and the schema is unchanged.** The script restores the
previous images, resets the checkout, and verifies the restored stack. You are back
where you started; the backup from step 1 is still on disk and untouched.

**Health does not come, and a migration removed something.** Restoring the old
images here would be worse than doing nothing: code written before a column was
dropped cannot run against a schema without it. The script therefore **refuses** the
image rollback and prints the manual recovery — reset the checkout, restore the
pre-upgrade dump, rebuild. It tells you at the START of the run when an upgrade is in
this category, so it is never a surprise at the end.

## Migrations

Schema changes are numbered SQL files applied in order, tracked in
`schema_migrations` by filename **and** sha256. Editing an already-applied migration
is a hard failure at boot, with the offending file named — because the alternative is
worse: it works on the author's machine and on a fresh install, and is wrong only on
an existing installation somebody else is running. Change a migration by adding a new
numbered file.

An empty migrations directory is also a hard failure. It means the image or the
checkout is broken, and running against an unmanaged schema fails later and somewhere
less obvious.

## Restoring a backup

Through the UI: **Settings -> Backup -> Restore**. By hand:

```sh
docker compose exec -T postgres pg_restore -U dida -d dida -c < path/to/dida-TIMESTAMP.dump
```

History (ClickHouse) is backed up separately — it is a firehose of tens of millions
of rows, not config, and it survives a rollback untouched. See Settings -> Backup.

## Downgrading

There is no supported downgrade. Migrations are forward-only. To go back, restore the
pre-upgrade dump and check out the commit it was taken on — which is what the refusal
path above prints for you.

## Upgrading from a very old version

Nothing special is required: the runner applies every migration you are missing, in
order, in one pass. That path — an old populated database meeting current code — is
covered by the test suite, and the resulting schema is asserted to be identical to a
fresh install.
