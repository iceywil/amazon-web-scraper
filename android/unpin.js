/*
 * Certificate-unpinning for traffic analysis on a local emulator.
 *
 * With the mitmproxy CA already in the system trust store, most TLS validates
 * fine; this only neutralises the *extra* pinning layers an app adds on top so
 * its own API traffic becomes readable for documentation. It hooks the usual
 * suspects and no-ops them. Attach with:
 *     frida -U -f <pkg> -l android/unpin.js
 */
'use strict';

Java.perform(function () {
    var hooked = [];
    function note(w) { if (hooked.indexOf(w) < 0) { hooked.push(w); console.log('[unpin] ' + w); } }

    // 1) OkHttp3 CertificatePinner.check() -> return (do nothing).
    try {
        var CP = Java.use('okhttp3.CertificatePinner');
        ['check', 'check$okhttp'].forEach(function (m) {
            if (CP[m]) {
                CP[m].overloads.forEach(function (ov) {
                    ov.implementation = function () { note('okhttp3.CertificatePinner.' + m); return; };
                });
            }
        });
    } catch (e) {}

    // 2) Platform TrustManagerImpl.verifyChain / checkTrustedRecursive -> return the chain unchecked.
    try {
        var TMI = Java.use('com.android.org.conscrypt.TrustManagerImpl');
        if (TMI.verifyChain) {
            TMI.verifyChain.implementation = function (chain) { note('conscrypt.TrustManagerImpl.verifyChain'); return chain; };
        }
        if (TMI.checkTrustedRecursive) {
            TMI.checkTrustedRecursive.implementation = function () {
                note('conscrypt.TrustManagerImpl.checkTrustedRecursive');
                return Java.use('java.util.ArrayList').$new();
            };
        }
    } catch (e) {}

    // 3) Replace every X509TrustManager installed via SSLContext.init with a permissive one.
    try {
        var X509TM = Java.use('javax.net.ssl.X509TrustManager');
        var SSLContext = Java.use('javax.net.ssl.SSLContext');
        var Permissive = Java.registerClass({
            name: 'org.local.PermissiveTM',
            implements: [X509TM],
            methods: {
                checkClientTrusted: function () {},
                checkServerTrusted: function () {},
                getAcceptedIssuers: function () { return []; }
            }
        });
        var init = SSLContext.init.overload(
            '[Ljavax.net.ssl.KeyManager;', '[Ljavax.net.ssl.TrustManager;', 'java.security.SecureRandom');
        init.implementation = function (km, tm, sr) {
            note('SSLContext.init (TrustManager replaced)');
            init.call(this, km, [Permissive.$new()], sr);
        };
    } catch (e) {}

    // 4) OkHttp OkHostnameVerifier.verify -> true.
    try {
        var OHV = Java.use('okhttp3.internal.tls.OkHostnameVerifier');
        OHV.verify.overload('java.lang.String', 'javax.net.ssl.SSLSession').implementation =
            function () { note('okhttp3 OkHostnameVerifier.verify'); return true; };
    } catch (e) {}

    console.log('[unpin] hooks installed; open a product/search screen to generate traffic');
});
