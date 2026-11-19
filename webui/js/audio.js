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
		$http.post(base + '/level', { level_pct: pct }, { timeout: 4000 }).then(function (r) {
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

	function poll() {
		$http.get(base + '/status.json', { timeout: 2500 }).then(function (r) {
			$scope.reachable = true;
			$scope.np = r.data.now_playing;
			$scope.h = r.data.health;
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
	$scope.$on('$destroy', function () { $interval.cancel(timer); if (sendTimer) clearTimeout(sendTimer); });
}
