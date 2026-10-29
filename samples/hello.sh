#!/usr/bin/env bash
# Prints a greeting and a small multiplication table.
echo "Hello, World!"
for i in 1 2 3 4 5; do
    echo "$i x 7 = $((i * 7))"
done
