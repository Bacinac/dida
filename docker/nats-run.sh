#!/bin/sh
# nats-server, reloaded whenever the keys service rewrites the user list: an `up`
# that adds an adapter must not leave it locked out until someone restarts the bus.
users=/etc/nats/auth/users.conf
nats-server -c /etc/nats/nats.conf &
pid=$!
trap 'kill -TERM "$pid"' TERM INT
seen=$(md5sum < "$users")
while kill -0 "$pid" 2>/dev/null; do
  sleep 2 & wait $!
  now=$(md5sum < "$users")
  if [ "$now" != "$seen" ]; then
    seen=$now
    kill -HUP "$pid"
  fi
done
wait "$pid"
