#!/bin/bash

cd ~/Dropbox/build/adsb-watch/
#./main.py --web --kml --fixed-lat 32.33432961922689 --fixed-lon -106.74182892861562 --fixed-alt-ft 6750 --govt-data-url https://data.n0gq.org --internet
./main.py --web --kml --fixed-lat 32.33432961922689 --fixed-lon -106.74182892861562 --fixed-alt-ft 6750 --govt-data-url http://10.1.17.20:8091 --internet
