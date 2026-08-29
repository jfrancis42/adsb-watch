#!/bin/bash

cd ~/Dropbox/build/adsb-watch/
#./main.py --web --kml --fixed-lat 40.25227441760919 --fixed-lon -105.82230428433839 --fixed-alt-ft 6750 --govt-data-url https://data.n0gq.org --internet
./main.py --web --kml --fixed-lat 40.25227441760919 --fixed-lon -105.82230428433839 --fixed-alt-ft 6750 --govt-data-url http://10.1.17.20:8091 --internet
