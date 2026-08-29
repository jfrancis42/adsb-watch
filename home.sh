#!/bin/bash

cd ~/Dropbox/build/adsb-watch/
#./main.py --web --kml --fixed-lat 39.3553696 --fixed-lon -104.6729929 --fixed-alt-ft 6750 --govt-data-url https://data.n0gq.org --internet
./main.py --web --kml --fixed-lat 39.3553696 --fixed-lon -104.6729929 --fixed-alt-ft 6750 --govt-data-url http://10.1.17.20:8091 --internet
