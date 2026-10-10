from setuptools import find_packages, setup

package_name = 'animatronic_common'

setup(
    name=package_name,
    version='0.0.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Maxwell Lokshin',
    maintainer_email='mlokshin@peakactivity.com',
    description='Shared snacks.json loader/validator used by brain, esp32_bridge, and speech.',
    license='Apache-2.0',
    tests_require=['pytest'],
)
