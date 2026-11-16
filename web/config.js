'use strict';

/* Site configuration.
 *
 * cartoKey: a CARTO Basemaps API key, for the street map under the "Basemap"
 * button. Free by e-mail from https://carto.com/basemaps/apikey/ -- required
 * for every request once keyless access ends on 30 November 2026.
 *
 * A key in a web page is public by nature: anyone can read it from the page
 * source. Restrict it to this site's address in the CARTO dashboard
 * (https://dashboard.basemaps.carto.com/) rather than treating it as a secret.
 */
window.APP_CONFIG = {
  cartoKey: '',
};
