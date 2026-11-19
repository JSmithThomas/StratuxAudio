// StratuxAudio: "Audio" page controller. Polls the status service on port 8090.
angular.module('appControllers').controller('AudioCtrl', AudioCtrl);
AudioCtrl.$inject = ['$scope', '$http', '$interval'];

function AudioCtrl($scope, $http, $interval) {
	$scope.$parent.helppage = 'plates/audio-help.html';
	var base = window.location.protocol + '//' + window.location.hostname + ':8090';
	$scope.np = { state: 'idle' };
	$scope.h = {};
	$scope.reachable = true;
	$scope.coverUrl = '';

	$scope.fmt = function (s) {
		if (s == null) return '';
		s = Math.floor(s);
		return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
	};
	$scope.allGood = function () {
		var h = $scope.h;
		return $scope.reachable && h.shairport == 'active' && h.avahi == 'active' && h.dongle && h.dongle.selected;
	};

	// Output level slider: follows the box until the user touches it, then sends
	// the new level half a second after they stop moving it.
	$scope.level = { pct: null, busy: false, msg: '', ok: true, touchedAt: 0 };
	var sendTimer = null;
	$scope.levelMoved = function () {
		$scope.level.touchedAt = Date.now();
		$scope.level.msg = '';
		if (sendTimer) clearTimeout(sendTimer);
		sendTimer = setTimeout(function () {
			$scope.$apply(function () { sendLevel(parseInt($scope.level.pct, 10)); });
		}, 500);
	};
	function sendLevel(pct) {
		$scope.level.busy = true;
		$http.post(base + '/level', { level_pct: pct }, { timeout: 15000 }).then(function (r) {
			$scope.level.ok = true;
			$scope.level.msg = r.data.saved ? 'saved' : 'set (not saved)';
		}, function (e) {
			$scope.level.ok = false;
			$scope.level.msg = (e.data && e.data.error) || 'could not set';
		}).finally(function () {
			$scope.level.busy = false;
			$scope.level.touchedAt = Date.now();
		});
	}

	// Equalizer and mono. Sliders follow the box until touched; a change is sent
	// 0.4 s after the last movement and applies live.
	$scope.eq = {};
	$scope.bands = [];
	$scope.eqPreset = '';
	$scope.eqLoudness = false;
	$scope.eqMsg = '';
	$scope.eqOk = true;
	$scope.mono = { on: null, busy: false };
	var eqTouchedAt = 0, eqTimer = null, eqBusy = false;

	$scope.hzLabel = function (hz) { return hz >= 1000 ? (hz / 1000) + ' kHz' : hz + ' Hz'; };
	// Curve drawing: x = log frequency 20 Hz - 20 kHz over 300 units, y = +-12 dB over 60.
	$scope.fx = function (hz) { return 300 * Math.log(hz / 20) / Math.log(1000); };
	$scope.fy = function (db) { return 30 - Math.max(-12, Math.min(12, db)) * 2.5; };
	$scope.curvePoints = function () {
		if (!$scope.eq.curve) return '';
		return $scope.eq.curve.map(function (p) {
			return $scope.fx(p[0]).toFixed(1) + ',' + $scope.fy(p[1]).toFixed(1);
		}).join(' ');
	};

	function sendEq() {
		eqBusy = true;
		$scope.eqOk = true;
		$scope.eqMsg = 'applying...';
		var body = {
			bands_db: $scope.bands.map(function (b) { return parseFloat(b.db); }),
			loudness: !!$scope.eqLoudness,
			preset: $scope.eqPreset
		};
		$http.post(base + '/eq', body, { timeout: 15000 }).then(function (r) {
			$scope.eq = angular.extend({}, $scope.eq, r.data.eq);
			$scope.eqOk = true;
			$scope.eqMsg = r.data.saved ? 'saved' : 'set (not saved)';
		}, function (e) {
			$scope.eqOk = false;
			$scope.eqMsg = (e.data && e.data.error) || 'could not set';
		}).finally(function () {
			eqBusy = false;
			eqTouchedAt = Date.now();
		});
	}
	$scope.bandMoved = function () {
		eqTouchedAt = Date.now();
		$scope.eqPreset = '';
		$scope.eqMsg = '';
		if (eqTimer) clearTimeout(eqTimer);
		eqTimer = setTimeout(function () { $scope.$apply(sendEq); }, 400);
	};
	$scope.applyPreset = function (name) {
		var p = $scope.eq.presets && $scope.eq.presets[name];
		if (!p) return;
		$scope.bands.forEach(function (b, i) { b.db = p[i]; });
		eqTouchedAt = Date.now();
		$scope.eqPreset = name;
		if (eqTimer) clearTimeout(eqTimer);
		sendEq();
	};
	$scope.setMono = function (on) {
		if ($scope.mono.on === on) return;
		$scope.mono.busy = true;
		$scope.eqMsg = 'switching...';
		$http.post(base + '/mono', { mono: on }, { timeout: 25000 }).then(function (r) {
			$scope.mono.on = r.data.mono;
			$scope.eqOk = true;
			$scope.eqMsg = 'saved';
		}, function (e) {
			$scope.eqOk = false;
			$scope.eqMsg = (e.data && e.data.error) || 'could not switch';
		}).finally(function () { $scope.mono.busy = false; });
	};

	function poll() {
		$http.get(base + '/status.json', { timeout: 2500 }).then(function (r) {
			$scope.reachable = true;
			$scope.np = r.data.now_playing;
			$scope.h = r.data.health;
			var a = r.data.audio;
			if (a) {
				if (!$scope.mono.busy) $scope.mono.on = a.mono;
				if (!eqBusy && Date.now() - eqTouchedAt > 3000) {
					$scope.eq = a.eq;
					$scope.bands = a.eq.bands.map(function (b) { return { hz: b.hz, db: b.db }; });
					$scope.eqPreset = a.eq.preset;
					$scope.eqLoudness = a.eq.loudness;
				}
			}
			var d = $scope.h.dongle;
			if (d && d.level_pct != null && !$scope.level.busy && Date.now() - $scope.level.touchedAt > 3000) {
				$scope.level.pct = d.level_pct;
			}
			$scope.coverUrl = $scope.np.has_cover ? base + '/cover?id=' + $scope.np.cover_id : '';
		}, function () {
			$scope.reachable = false;
		});
	}
	poll();
	var timer = $interval(poll, 2000);
	$scope.$on('$destroy', function () {
		$interval.cancel(timer);
		if (sendTimer) clearTimeout(sendTimer);
		if (eqTimer) clearTimeout(eqTimer);
	});
}
